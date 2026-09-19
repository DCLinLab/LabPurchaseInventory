"""One durable photo-analysis queue, with separate capture and delivery records."""

import json
import logging
import re
import time
from pathlib import Path
from threading import Event, Thread

from label_reader import ReaderError, render_reply, slack_payload
from photo_intake import write_json


LOG = logging.getLogger("labpurchase")


class LabelWorker:
    def __init__(self, root, channel_id, client, reader, clock=time.time, receipt_reply=None):
        self.root = Path(root)
        self.channel_id, self.client, self.reader, self.clock = channel_id, client, reader, clock
        self.stop = Event()
        self.thread = None
        self.receipt_reply = receipt_reply

    def start(self):
        self.thread = Thread(target=self.run, name="package-label-reader", daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            for manifest in sorted(self.root.glob(f"{self.channel_id}_*/record.json")):
                if self.stop.is_set():
                    return
                try:
                    self.process(manifest)
                except Exception as error:
                    LOG.error("Label queue item failed (%s).", type(error).__name__)
            self.stop.wait(5)

    def process(self, manifest, deliver=True):
        manifest = Path(manifest)
        record = json.loads(manifest.read_text(encoding="utf-8"))
        if record["channel_id"] != self.channel_id or not re.fullmatch(r"\d+\.\d+", record["thread_ts"]):
            raise ReaderError("invalid_queue_destination")
        if record["status"] not in {"awaiting_ai", "partial"}:
            return None
        state_path = manifest.with_name("analysis.json")
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
            "status": "pending", "attempts": 0, "inventory_change_applied": False,
        }
        # An interrupted HTTP send may have reached Slack. Do not blindly post again.
        if state["status"] == "posting":
            state["status"] = "delivery_uncertain"
            write_json(state_path, state)
            LOG.error("Slack reply delivery uncertain ts=%s; check thread before retrying.", record["message_ts"])
        if state["status"] in {"sent", "failed", "delivery_uncertain", "skipped"}:
            return state
        if self.clock() < state.get("retry_at", 0):
            return state
        if state["status"] not in {"ready", "failed_ready"}:
            cooldown_path = self.root / "reader-cooldown.json"
            cooldown = json.loads(cooldown_path.read_text(encoding="utf-8")) if cooldown_path.exists() else {}
            if self.clock() < cooldown.get("retry_at", 0):
                return state
            if hasattr(self.reader, "ready") and not self.reader.ready():
                if state["status"] != "waiting_configuration":
                    LOG.warning("Photo queued ts=%s; waiting for reader authentication.", record["message_ts"])
                state.update(status="waiting_configuration", retry_at=self.clock() + 30)
                write_json(state_path, state)
                return state
            if state["attempts"] >= 3:
                state.update(status="failed_ready", error="reader_retry_limit")
                write_json(state_path, state)
                return state
            state.update(status="analyzing", attempts=state["attempts"] + 1)
            write_json(state_path, state)
            LOG.info("Reading package label ts=%s attempt=%s", record["message_ts"], state["attempts"])
            try:
                state["result"] = self.reader.read(record, manifest.parent)
                state.update(status="ready", completed_at=self.clock())
                state.pop("error", None)
                state.pop("retry_at", None)
            except Exception as error:
                code = str(error) if isinstance(error, ReaderError) else "reader_internal_error"
                if code in {"codex_usage_limit", "api_usage_limit"}:
                    # Quota exhaustion is temporary, not a permanently failed photo.
                    # Persist one cooldown for the whole queue, including restarts.
                    retry_at = self.clock() + 1800
                    write_json(cooldown_path, {"retry_at": retry_at, "error": code})
                    state.update(status="waiting_usage", attempts=state["attempts"] - 1,
                                 error=code, retry_at=retry_at)
                    write_json(state_path, state)
                    LOG.warning("Label reading paused for usage limits; photos remain queued.")
                    return state
                state.update(status="failed_ready" if state["attempts"] >= 3 else "retry_wait", error=code,
                             retry_at=self.clock() + (1800 if code in {"codex_usage_limit", "api_usage_limit"} else 60 * state["attempts"]))
                if state["status"] == "failed_ready":
                    state.pop("retry_at", None)
                write_json(state_path, state)
                LOG.error("Label reading ts=%s failed: %s", record["message_ts"], code)
                return state
            write_json(state_path, state)
            LOG.info("Package label ready ts=%s", record["message_ts"])
        if deliver and state["status"] in {"ready", "failed_ready"}:
            failed = state["status"] == "failed_ready"
            reply = ("I saved the package photos, but automatic label reading failed after three attempts. "
                     "Please have the workstation operator check the reader's login or connection. "
                     "Stock counts have not changed.") if failed else render_reply(state["result"], record)
            if not reply:
                state.update(status='skipped', reason='no_clear_delivery_evidence')
                write_json(state_path, state)
                return state
            links = None
            if not failed and self.receipt_reply is not None:
                try:
                    prepared = self.receipt_reply.prepare(state['result'],record,manifest)
                    if not prepared:
                        raise ReaderError('missing_receipt_reply')
                    reply,links = prepared['text'],prepared['links']
                    state['receipt_context'] = prepared
                    state.pop('reply_error',None)
                    state.pop('retry_at',None)
                except Exception as error:
                    state.update(status='ready',reply_error=type(error).__name__,retry_at=self.clock()+60)
                    write_json(state_path,state)
                    LOG.warning('Receipt reply waiting for sheet context ts=%s error=%s',record['message_ts'],type(error).__name__)
                    return state
            state.update(status="posting", reply_text=reply)
            write_json(state_path, state)
            try:
                response = self.client.chat_postMessage(channel=self.channel_id, thread_ts=record["thread_ts"],
                                                        **slack_payload(reply, links))
                state.update(status="failed" if failed else "sent", reply_ts=response["ts"], sent_at=self.clock())
            except Exception:
                state.update(status="delivery_uncertain")
                LOG.error("Slack reply delivery uncertain ts=%s; check thread before retrying.", record["message_ts"])
            write_json(state_path, state)
            if state["status"] == "sent":
                LOG.info("Package label reply sent ts=%s reply=%s", record["message_ts"], state["reply_ts"])
        return state
