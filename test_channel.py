"""Explicit test channels: isolated local queues and read-only sheet previews."""

import os
import re
from dataclasses import replace
from pathlib import Path

from inventory_sync import GoogleInventoryStore
from label_reader import CodexLabelReader
from label_worker import LabelWorker
from order_sync import GoogleOrderStore
from photo_intake import PhotoIntake
from receipt_quantity import quantity_candidates
from receipt_reply import build_reply, live_orders
from runtime_lock import InstanceLock
from sheet_sync import GoogleReceiptStore, SheetSyncError, receipt_rows
from status_queries import StatusQueryWorker


TEST_NOTICE = 'TEST MODE — real inventory is unchanged.\n'


def configured_test_channels(production_channel):
    channels = list(dict.fromkeys(filter(None, re.split(r'[,\s]+', os.environ.get('SLACK_TEST_CHANNEL_IDS', '').strip()))))
    if any(not re.fullmatch(r'C[A-Z0-9]+', channel) or channel == production_channel for channel in channels):
        raise ValueError('Test channel IDs must be valid and separate from the production channel.')
    return channels


class ReadOnlyRequests:
    def request(self, method, url, **kwargs):
        if method.upper() != 'GET':
            raise SheetSyncError('test_channel_write_forbidden')
        return super().request(method, url, **kwargs)


class ReadOnlyOrders(ReadOnlyRequests, GoogleOrderStore):
    pass


class ReadOnlyInventory(ReadOnlyRequests, GoogleInventoryStore):
    pass


class ReadOnlyReceipts(ReadOnlyRequests, GoogleReceiptStore):
    pass


class PreviewReceiptReply:
    def __init__(self, local_root, config):
        self.local_root, self.config = Path(local_root), config
        self.orders = ReadOnlyOrders(config)
        self.inventory = ReadOnlyInventory(config)

    def prepare(self, result, record, manifest):
        lock = InstanceLock(self.local_root / 'sheet-sync.lock')
        lock.acquire(timeout=45)
        try:
            orders = live_orders(self.orders.snapshot())
            inventory = self.inventory.snapshot()
        finally:
            lock.release()
        # Receipt rows exist only in memory. No sheet writer or sync worker is used.
        rows = receipt_rows(record, result)
        quantities = quantity_candidates(Path(manifest).parent.parent, orders.values())
        for row in rows:
            quantity = quantities.get(row[0])
            if quantity:
                row[8:10] = [quantity['quantity'], quantity['unit']]
        reply = build_reply(result, record, orders, inventory,
                            dict(enumerate(rows, start=5)), self.config, preview=True)
        if reply:
            reply['text'] = TEST_NOTICE + reply['text']
            reply['test_mode'] = True
        return reply


def start_test_channel(local_root, settings, channel, bot_user_id, client, config, label_enabled):
    from slack_bot import MessageReceiver
    if channel == settings.channel_id or not re.fullmatch(r'C[A-Z0-9]+', channel):
        raise ValueError('invalid_test_channel')
    if not config:
        raise ValueError('Test channels require sheet configuration for read-only context.')
    local_root = Path(local_root)
    intake = PhotoIntake(local_root / ('test-intake-' + channel))
    query = StatusQueryWorker(local_root / ('test-queries-' + channel), channel, client, config,
                              reply_prefix=TEST_NOTICE + 'Queries show real sheet data; test photos are excluded.\n')
    query.orders, query.inventory, query.receipts = ReadOnlyOrders(config), ReadOnlyInventory(config), ReadOnlyReceipts(config)
    workers = [query]
    if label_enabled:
        workers.append(LabelWorker(intake.root, channel, client, CodexLabelReader(),
                                   receipt_reply=PreviewReceiptReply(local_root, config)))
    receiver = MessageReceiver(replace(settings, channel_id=channel), bot_user_id, intake, query,
                               pong=TEST_NOTICE + 'Pong! Send a package photo with its total count, or ask about orders/inventory, without an @mention. Questions read the real sheet; test photos produce previews only.')
    for worker in workers:
        worker.start()
    return receiver, workers
