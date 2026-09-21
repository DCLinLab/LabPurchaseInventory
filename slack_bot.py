"""Slack package-photo intake and Codex ChatGPT-login label reading."""

import argparse
import logging
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from threading import Lock

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from photo_intake import PhotoIntake
from label_reader import CodexLabelReader
from label_worker import LabelWorker
from runtime_lock import InstanceLock
from sheet_sync import configured_sync
from order_sync import configured_order_worker


ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("labpurchase")
PONG = (
    "Pong! LabPurchaseBot is connected on the lab workstation. "
    "I can receive messages here without an @mention. "
    "Package photos are saved for label reading; received quantities are inferred when package counts and contents are clear."
)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    app_token: str
    team_id: str
    app_id: str
    channel_id: str

    @classmethod
    def from_environment(cls):
        load_dotenv(ROOT / ".env", override=False)
        values = {}
        for field in cls.__dataclass_fields__:
            key = f"SLACK_{field.upper()}"
            value = os.environ.get(key, "").strip()
            if not value:
                raise ValueError(f"Set {key} in the local .env file.")
            values[field] = value
        for field, prefix in (("bot_token", "xoxb-"), ("app_token", "xapp-")):
            if not values[field].startswith(prefix):
                raise ValueError(f"SLACK_{field.upper()} must start with {prefix}.")
        return cls(**values)


def is_channel_message(body, settings, bot_user_id):
    """Accept new human messages, including ordinary thread/file messages."""
    event = body.get("event", {})
    return (
        body.get("team_id") == settings.team_id
        and body.get("api_app_id") == settings.app_id
        and event.get("channel") == settings.channel_id
        and event.get("type") == "message"
        and bool(event.get("user"))
        and event.get("user") != bot_user_id
        and not event.get("bot_id")
        and not event.get("app_id")
        and event.get("subtype") in (None, "file_share", "thread_broadcast", "me_message")
        and bool(event.get("ts"))
    )


class MessageReceiver:
    def __init__(self, settings, bot_user_id, photo_intake=None, query_worker=None, pong=PONG):
        self.settings = settings
        self.bot_user_id = bot_user_id
        self.photo_intake = photo_intake
        self.query_worker = query_worker
        self.pong = pong
        self._seen = OrderedDict()
        self._lock = Lock()

    def receive(self, body, client):
        if not is_channel_message(body, self.settings, self.bot_user_id):
            return
        event = body["event"]
        # Deduplicate repeated deliveries and thread broadcasts for this process.
        key = (event["channel"], event["ts"])
        with self._lock:
            if key in self._seen:
                return
            self._seen[key] = None
            if len(self._seen) > 4096:
                self._seen.popitem(last=False)
        LOG.info("Received channel message ts=%s thread=%s", event["ts"], event.get("thread_ts", "-"))
        if self.query_worker is not None and self.query_worker.capture(event):
            LOG.info('Status query queued ts=%s',event['ts'])
            return
        try:
            if self.photo_intake is not None and event.get("files"):
                record = self.photo_intake.capture(event, client, self.settings.bot_token)
                if record:
                    LOG.info("Photo intake ts=%s status=%s photos=%s", event["ts"], record["status"], len(record["files"]))
            if event.get("text", "").strip().casefold() not in {"ping", "test"}:
                return
            client.chat_postMessage(
                channel=self.settings.channel_id,
                thread_ts=event.get("thread_ts") or event["ts"],
                text=self.pong,
            )
            LOG.info("Connection-test reply sent.")
        except Exception:
            with self._lock:
                self._seen.pop(key, None)
            raise


class RedactSecrets(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        record.msg = re.sub(r"(?:xox[baprs]-|xapp-|sk-)[^\s,<>\"']+", "[REDACTED]", message)
        record.args = ()
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Check bot identity and channel access, then exit without sending messages.")
    mode.add_argument("--capture-message", metavar="TIMESTAMP", help="Save photos from one existing target-channel message without sending a reply.")
    mode.add_argument("--analyze-message", metavar="TIMESTAMP", help="Read a previously captured message using the Codex login, without posting to Slack.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactSecrets())
    instance = InstanceLock(ROOT / ".local" / "slack-bot.lock")
    try:
        if not args.check:
            instance.acquire()
        settings = Settings.from_environment()
        from test_channel import configured_test_channels, start_test_channel
        test_channels = configured_test_channels(settings.channel_id)
        client = WebClient(token=settings.bot_token)
        identity = client.auth_test()
        if identity.get("team_id") != settings.team_id or not identity.get("bot_id"):
            raise ValueError("The token is not a bot in the configured Slack workspace.")
        # Requires only the channels:history scope already granted to the bot.
        client.conversations_history(channel=settings.channel_id, limit=1)
        LOG.info("Bot identity and target channel access verified: team=%s user=%s channel=%s", settings.team_id, identity["user_id"], settings.channel_id)
        for channel in test_channels:
            client.conversations_history(channel=channel, limit=1)
            LOG.info('Test channel access verified: channel=%s', channel)
        if args.check:
            LOG.info("Socket token syntax checked; its connection will be verified when the listener starts.")
            return 0
        intake = PhotoIntake(ROOT / ".local" / "intake")
        if args.analyze_message:
            if not re.fullmatch(r"\d+\.\d{6}", args.analyze_message):
                raise ValueError("Provide the exact Slack timestamp enclosed in quotes.")
            reader = CodexLabelReader()
            reader.check_login()
            worker = LabelWorker(intake.root, settings.channel_id, client, reader)
            state = worker.process(intake.root / f"{settings.channel_id}_{args.analyze_message}" / "record.json", deliver=False)
            LOG.info("Label analysis status=%s", state["status"] if state else "not_ready")
            return 0 if state and state["status"] in {"ready", "sent"} else 1
        if args.capture_message:
            if not re.fullmatch(r"\d+\.\d{6}", args.capture_message):
                raise ValueError("Provide the exact Slack timestamp with six decimal digits, enclosed in quotes so PowerShell does not round it.")
            # Fetch a narrow window, then select by the original timestamp
            # string. Slack timestamps are identifiers, not floating-point values.
            stamp = Decimal(args.capture_message)
            messages = client.conversations_history(channel=settings.channel_id, oldest=str(stamp - 1), latest=str(stamp + 1), inclusive=True, limit=100)["messages"]
            event = next((item for item in messages if item.get("ts") == args.capture_message), None)
            if event is None:
                raise ValueError("The selected message was not found.")
            event["channel"] = settings.channel_id
            body = {"team_id": settings.team_id, "api_app_id": settings.app_id, "event": event}
            if not is_channel_message(body, settings, identity["user_id"]):
                raise ValueError("The selected message is not an eligible channel message.")
            record = intake.capture(event, client, settings.bot_token)
            if not record:
                raise ValueError("The selected message has no supported photo attachments.")
            LOG.info("Photo intake ts=%s status=%s photos=%s", event["ts"], record["status"], len(record["files"]))
            return 0 if record["status"] == "awaiting_ai" else 1
        app = App(token=settings.bot_token)
        sheet_worker = configured_sync(settings.channel_id)
        query_worker = None
        if sheet_worker:
            sheet_worker.start()
            LOG.info("Automatic package receipt sheet sync enabled.")
            from status_queries import StatusQueryWorker
            query_worker = StatusQueryWorker(ROOT/'.local'/'queries',settings.channel_id,client,sheet_worker.store.config)
            query_worker.start()
            LOG.info('Flexible read-only lab analysis enabled; live sheet and parsed email snapshots; model=gpt-5.6-luna reasoning=low.')
        receiver = MessageReceiver(settings,identity['user_id'],intake,query_worker)
        order_worker = configured_order_worker(client,settings.channel_id)
        if order_worker:
            order_worker.start()
            LOG.info("Semantic forwarded order email sync enabled; Luna reads varied suppliers, text, PDFs and image attachments.")
            if order_worker.notifier:
                LOG.info('Order email status notifications enabled for channel=%s.',settings.channel_id)
        worker = None
        if os.environ.get("LABEL_READER_ENABLED", "false").lower() == "true":
            reader = CodexLabelReader()
            reply_builder = None
            if sheet_worker:
                from receipt_reply import ReceiptReply
                reply_builder = ReceiptReply(intake.root,settings.channel_id,sheet_worker.store.config)
                LOG.info('Package replies include verified live order and inventory context.')
            worker = LabelWorker(intake.root, settings.channel_id, client, reader, receipt_reply=reply_builder)
            worker.start()
            LOG.info("Background label reader uses Codex ChatGPT login; model=%s ready=%s. Shared Codex allowance; no API-key fallback.", reader.model, reader.ready())

        receivers = {settings.channel_id: receiver}
        test_workers = []
        for channel in test_channels:
            test_receiver, workers = start_test_channel(
                ROOT / '.local', settings, channel, identity['user_id'], client,
                sheet_worker.store.config if sheet_worker else None,
                label_enabled=worker is not None)
            receivers[channel] = test_receiver
            test_workers.extend(workers)
            LOG.info('Test channel enabled: channel=%s; isolated photos, read-only real sheet context.', channel)

        @app.event("message")
        def on_message(body, client):
            selected = receivers.get(body.get('event', {}).get('channel'))
            if selected:
                selected.receive(body, client)

        @app.error
        def on_error(error, logger):
            # Do not dump event bodies, tokens, or message content into logs.
            code = error.response.get("error", "unknown") if isinstance(error, SlackApiError) else type(error).__name__
            logger.error("Message handling failed: %s", code)

        LOG.info("Starting channel listener; package label reader enabled=%s.", worker is not None)
        try:
            SocketModeHandler(app, settings.app_token).start()
        finally:
            for test_worker in test_workers:
                test_worker.stop.set()
            if worker:
                worker.stop.set()
            if sheet_worker:
                sheet_worker.stop.set()
            if query_worker:
                query_worker.stop.set()
            if order_worker:
                order_worker.stop.set()
        return 0
    except ValueError as error:
        LOG.error("%s", error)
    except SlackApiError as error:
        LOG.error("Slack connection failed: %s", error.response.get("error", "unknown"))
    except KeyboardInterrupt:
        LOG.info("Stopped.")
        return 0
    except Exception as error:
        LOG.error("Connection failed (%s). Check network access and local configuration.", type(error).__name__)
    finally:
        instance.release()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
