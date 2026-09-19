"""Durable, independent delivery of cached label observations to the lab sheet."""

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
from threading import Event, Thread
import time
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession
from google_auth import EXPECTED_EMAIL, load_credentials
from label_reader import validate_result
from message_intent import is_delivery_item, legacy_shortage
from photo_intake import write_json
from runtime_lock import InstanceLock

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger('labpurchase.sheets')
TAB = 'Package receipts'
HEADERS = ['Receipt ID', 'Slack posted at', 'Posted by', 'Product', 'Supplier / brand',
           'Catalog number', 'Specifications', 'Printed pack size', 'Quantity received',
           'Unit', 'Intended storage', 'Confirmed storage', 'Lot / serial', 'Expiry date',
           'Tracking number', 'Order ID', 'Slack message link', 'Notes']
EPOCH = date(1899, 12, 30)


class SheetSyncError(ValueError):
    """Fixed non-secret error codes only."""


def receipt_rows(record, result):
    if legacy_shortage(record, result): return []
    fields = validate_result(result['fields'], [f['file_id'] for f in record['files']
                                               if f.get('status') == 'downloaded'])
    channel, stamp = record['channel_id'], record['message_ts']
    if not re.fullmatch(r'C[A-Z0-9]+', channel) or not re.fullmatch(r'\d+\.\d+', stamp):
        raise SheetSyncError('invalid_receipt_source')
    posted = datetime.fromtimestamp(int(stamp.split('.')[0]), timezone.utc)
    posted_serial = (posted.date() - EPOCH).days + (posted.hour * 3600 + posted.minute * 60 + posted.second) / 86400
    caption = fields['caption_interpretation']
    rows = []
    for index, item in enumerate(fields['items']):
        if not is_delivery_item(item):
            continue
        key = f'{channel}:{stamp}:{index}'
        receipt_id = 'R-' + hashlib.sha256(key.encode()).hexdigest()[:20]
        notes = [f"Label: {item['label_type']}; readability: {item['confidence']}",
                 'Source files: ' + ', '.join(item['source_file_ids']), 'Slack timestamp shown in UTC']
        notes.append('Delivery evidence: ' + item['receipt_assessment']['evidence'])
        if caption['received_quantity_statement']:
            # This is a message-level statement: do not assign it to every item
            # or reinterpret the pack size as a count.
            notes.append('Message receipt statement: ' + caption['received_quantity_statement'])
        if item['carrier']:
            notes.append('Carrier: ' + item['carrier'])
        if item['expiry_printed']:
            notes.append('Printed expiry: ' + item['expiry_printed'])
        if any(f.get('status') == 'failed' for f in record['files']):
            notes.append('Some attachments could not be read')
        expiry = (date.fromisoformat(item['expiry_iso']) - EPOCH).days if item['expiry_iso'] else ''
        row = [receipt_id, posted_serial, record['user_id'], item['product'], item['brand_or_supplier'],
               item['catalog_number'], item['specifications'], item['packaging_text'], '', '',
               caption['intended_storage'], caption['confirmed_storage'], item['lot_or_serial'], expiry,
               item['tracking_number'], item['order_reference'],
               f'https://slack.com/archives/{channel}/p{stamp.replace(".", "")}', '; '.join(notes)]
        rows.append([value if value is not None else '' for value in row])
    return rows


class GoogleReceiptStore:
    def __init__(self, config, session_factory=None):
        self.config = config
        if config.get('account') != EXPECTED_EMAIL or not re.fullmatch(r'[A-Za-z0-9_-]+', config['spreadsheet_id']):
            raise SheetSyncError('invalid_sheet_configuration')
        self.sheet_id = config['tabs'][TAB]['sheet_id']
        self.base = 'https://sheets.googleapis.com/v4/spreadsheets/' + config['spreadsheet_id']
        self.session_factory = session_factory or (lambda: AuthorizedSession(load_credentials()))
        self.session = None

    def request(self, method, url, **kwargs):
        if self.session is None:
            self.session = self.session_factory()
        try:
            response = self.session.request(method, url, timeout=30, **kwargs)
        except Exception as error:
            self.session.close()
            self.session = None
            raise SheetSyncError('sheet_connection_failed') from error
        if not response.ok:
            code = 'google_authorization_required' if response.status_code in (401, 403) else 'sheet_request_failed'
            if response.status_code in (401, 403):
                self.session.close()
                self.session = None
            raise SheetSyncError(code)
        return response.json()

    def values(self, address):
        return self.request('GET', self.base + '/values/' + quote(f"'{TAB}'!{address}", safe=''),
                            params={'valueRenderOption': 'UNFORMATTED_VALUE'}).get('values', [])

    def snapshot(self):
        meta = self.request('GET', self.base, params={'fields': 'sheets(properties)'})
        sheet = next((s['properties'] for s in meta['sheets'] if s['properties']['sheetId'] == self.sheet_id), None)
        if not sheet or sheet['title'] != TAB or sheet['gridProperties']['columnCount'] < len(HEADERS):
            raise SheetSyncError('receipt_tab_changed')
        self.row_count = sheet['gridProperties']['rowCount']
        if self.values('A4:R4') != [HEADERS]:
            raise SheetSyncError('receipt_headers_changed')
        # Bounded pages also detect manually entered rows with an empty receipt ID.
        rows = {}
        for start in range(5, self.row_count + 1, 1000):
            page = self.values(f'A{start}:R{min(start + 999, self.row_count)}')
            rows.update({start + i: row for i, row in enumerate(page) if any(v != '' for v in row)})
        return rows

    def read_row(self, row):
        if row > self.row_count:
            return []
        return next(iter(self.values(f'A{row}:R{row}')), [])

    def write_row(self, row, values):
        if row > self.row_count:
            # A retry reads fresh metadata, so a lost resize response does not
            # append another batch of blank rows.
            self.request('POST', self.base + ':batchUpdate', json={'requests': [{
                'updateSheetProperties': {'properties': {'sheetId': self.sheet_id,
                    'gridProperties': {'rowCount': row + 99}}, 'fields': 'gridProperties.rowCount'}}]})
            self.row_count = row + 99
        # Guard against formulas, validations and a user occupying the reserved row.
        cells = self.request('GET', self.base, params={
            'ranges': f"'{TAB}'!A{row}:R{row}", 'includeGridData': 'true',
            'fields': 'sheets(data(rowData(values(userEnteredValue,dataValidation))))'})
        for sheet in cells.get('sheets', []):
            for data in sheet.get('data', []):
                for data_row in data.get('rowData', []):
                    for cell in data_row.get('values', []):
                        if cell.get('userEnteredValue') or cell.get('dataValidation'):
                            raise SheetSyncError('reserved_receipt_row_changed')
        # Explicit stringValue prevents text beginning with '=' from becoming
        # a formula. Values and formatting commit atomically in the same batch.
        # Only the new row receives formatting; neighboring content is untouched.
        base_range = {'sheetId': self.sheet_id, 'startRowIndex': row - 1, 'endRowIndex': row}
        requests = [{'updateCells': {'start': {'sheetId': self.sheet_id, 'rowIndex': row - 1, 'columnIndex': 0},
                     'rows': [{'values': [{'userEnteredValue': {'numberValue': value} if isinstance(value, (int, float))
                                          else {'stringValue': value}} for value in values]}],
                     'fields': 'userEnteredValue'}},
                    {'repeatCell': {'range': {**base_range, 'startColumnIndex': 0, 'endColumnIndex': 18},
                     'cell': {'userEnteredFormat': {'wrapStrategy': 'WRAP', 'verticalAlignment': 'TOP',
                              'textFormat': {'fontFamily': 'Arial', 'fontSize': 10}}},
                     'fields': 'userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment,userEnteredFormat.textFormat'}},
                    {'repeatCell': {'range': {**base_range, 'startColumnIndex': 1, 'endColumnIndex': 2},
                     'cell': {'userEnteredFormat': {'numberFormat': {'type': 'DATE_TIME', 'pattern': 'yyyy-mm-dd hh:mm'}}},
                     'fields': 'userEnteredFormat.numberFormat'}},
                    {'repeatCell': {'range': {**base_range, 'startColumnIndex': 13, 'endColumnIndex': 14},
                     'cell': {'userEnteredFormat': {'numberFormat': {'type': 'DATE', 'pattern': 'yyyy-mm-dd'}}},
                     'fields': 'userEnteredFormat.numberFormat'}},
                    {'updateDimensionProperties': {'range': {'sheetId': self.sheet_id, 'dimension': 'ROWS',
                      'startIndex': row - 1, 'endIndex': row}, 'properties': {'pixelSize': 100}, 'fields': 'pixelSize'}}]
        if row > 104:
            requests.append({'setBasicFilter': {'filter': {'range': {'sheetId': self.sheet_id,
                'startRowIndex': 3, 'endRowIndex': self.row_count, 'startColumnIndex': 0, 'endColumnIndex': 18}}}})
        self.request('POST', self.base + ':batchUpdate', json={'requests': requests})


class ReceiptSync:
    def __init__(self, root, channel_id, store, clock=time.time):
        self.root, self.channel_id, self.store, self.clock = Path(root), channel_id, store, clock
        self.stop = Event()
        self.thread = None

    def process(self, manifest):
        path = manifest.with_name('sheet-sync.json')
        state = json.loads(path.read_text()) if path.exists() else {'status': 'pending', 'reservations': {}}
        if state.get('spreadsheet_id') not in (None, self.store.config['spreadsheet_id']):
            raise SheetSyncError('receipt_destination_changed')
        if state['status'] in ('synced', 'skipped') or self.clock() < state.get('retry_at', 0):
            return state
        analysis_path = manifest.with_name('analysis.json')
        if not analysis_path.exists():
            return state
        analysis = json.loads(analysis_path.read_text(encoding='utf-8'))
        if 'result' not in analysis or analysis['status'] not in ('ready', 'posting', 'sent', 'delivery_uncertain'):
            return state
        record = json.loads(manifest.read_text(encoding='utf-8'))
        if record['channel_id'] != self.channel_id:
            raise SheetSyncError('wrong_receipt_channel')
        try:
            rows = receipt_rows(record, analysis['result'])
            state['spreadsheet_id'] = self.store.config['spreadsheet_id']
            if not rows:
                state.update(status='skipped', reason='no_package_labels')
            else:
                remote = self.store.snapshot()
                ids = [r[0] for r in remote.values() if r and r[0]]
                if len(ids) != len(set(ids)):
                    raise SheetSyncError('duplicate_remote_receipt_ids')
                for values in rows:
                    receipt_id = values[0]
                    if receipt_id in ids:
                        continue  # Also preserves manual edits and rows moved by sorting.
                    row = state['reservations'].get(receipt_id)
                    if row is None:
                        row = max([4, *remote.keys(), *state['reservations'].values()]) + 1
                        state['reservations'][receipt_id] = row
                        state['status'] = 'writing'
                        write_json(path, state)  # Reserve before sending; retries reuse the row.
                    current = self.store.read_row(row)
                    if any(v != '' for v in current):
                        raise SheetSyncError('reserved_receipt_row_changed')
                    self.store.write_row(row, values)
                    actual = self.store.read_row(row)
                    if actual + [''] * (18 - len(actual)) != values:
                        raise SheetSyncError('receipt_readback_mismatch')
                    remote[row] = values
                    ids.append(receipt_id)
                state.update(status='synced', synced_at=self.clock(), receipt_ids=[r[0] for r in rows])
                state.pop('error', None)
                state.pop('retry_at', None)
            write_json(path, state)
            LOG.info('Receipt sheet status=%s ts=%s', state['status'], record['message_ts'])
        except Exception as error:
            code = str(error) if isinstance(error, SheetSyncError) else type(error).__name__
            state.update(status='retry_wait', error=code, retry_at=self.clock() + 300)
            write_json(path, state)
            LOG.warning('Receipt sheet queued ts=%s error=%s', record['message_ts'], code)
        return state

    def run_once(self):
        with_lock = InstanceLock(self.root.parent / 'sheet-sync.lock')
        with_lock.acquire()
        try:
            states = [self.process(p) for p in sorted(self.root.glob(f'{self.channel_id}_*/record.json'))]
            if isinstance(self.store, GoogleReceiptStore):
                from receipt_quantity import sync_quantities
                orders_path = self.root.parent / 'email-intake' / 'orders.json'
                orders = json.loads(orders_path.read_text(encoding='utf-8')).get('orders', {}).values() if orders_path.exists() else ()
                sync_quantities(self.root, self.store, orders)
                if self.store.config.get('inventory_sync_enabled'):
                    from inventory_sync import sync_inventory
                    sync_inventory(self.root, self.store)
            return states
        finally:
            with_lock.release()

    def run(self):
        while not self.stop.is_set():
            try:
                self.run_once()
            except Exception as error:
                LOG.warning('Sheet queue pass failed (%s)', type(error).__name__)
            self.stop.wait(30)

    def start(self):
        self.thread = Thread(target=self.run, name='package-sheet-sync', daemon=True)
        self.thread.start()


def configured_sync(channel_id):
    path = ROOT / '.local' / 'google-sheet.json'
    config = json.loads(path.read_text()) if path.exists() else {}
    if not config.get('automatic_sync_enabled'):
        return None
    return ReceiptSync(ROOT / '.local' / 'intake', channel_id, GoogleReceiptStore(config))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', required=True)
    parser.parse_args()
    from slack_bot import Settings
    logging.basicConfig(level=logging.INFO)
    worker = configured_sync(Settings.from_environment().channel_id)
    if worker is None:
        raise SystemExit('Enable automatic_sync_enabled in .local/google-sheet.json first.')
    states = worker.run_once()
    print(json.dumps({'statuses': [s['status'] for s in states]}))
    raise SystemExit(1 if any(s['status'] == 'retry_wait' for s in states) else 0)
