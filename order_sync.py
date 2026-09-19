"""Forwarded purchase mail to Orders, with conservative receipt matching."""

import argparse
from datetime import date
import json
import logging
from pathlib import Path
from threading import Event, Thread
import time
from urllib.parse import quote

from order_email import GmailOrderIntake, OrderEmailError, normalize, parse_pending, parsed_documents
from photo_intake import write_json
from runtime_lock import InstanceLock
from sheet_sync import EPOCH, GoogleReceiptStore, ROOT, SheetSyncError


LOG = logging.getLogger('labpurchase.orders')
HEADERS = ['Order ID', 'Order date', 'Requester', 'Supplier', 'Product', 'Catalog number',
           'Specifications', 'Quantity ordered', 'Unit', 'Order progress', 'Tracking number',
           'Source email link', 'Notes']


def unique(values, name):
    values = list(dict.fromkeys(v for v in values if v is not None and v != ''))
    if len(values) > 1:
        raise OrderEmailError('conflicting_' + name)
    return values[0] if values else None


def order_key(supplier, order_id, catalog):
    return '|'.join(normalize(v) for v in (supplier, order_id, catalog))


def merge_documents(documents):
    groups = {}
    for document in documents:
        for item in document['items']:
            key = order_key(document['supplier'], document['order_id'], item['catalog'])
            groups.setdefault(key, []).append((document, item))
    merged, conflicts = {}, {}
    for key, records in groups.items():
        try:
            if any(d.get('semantic_version') for d, _ in records):
                from order_reconcile import merge_line
                merged[key] = merge_line(records)
                continue
            docs, items = zip(*records)
            order = {field: unique([d.get(field) for d in docs], field) for field in
                     ('supplier', 'order_id', 'purchase_order', 'order_date', 'requester')}
            order.update({field: unique([i.get(field) for i in items], field) for field in
                         ('quantity_ordered', 'unit', 'pack_size', 'specifications')})
            order['catalog'] = items[0]['catalog']
            # Prefer the supplier's descriptive product name from shipping mail.
            order['product'] = next((i['product'] for d, i in records if d['kind'] == 'shipping_confirmation'), items[0]['product'])
            shipments = {}
            for item in items:
                shipments.setdefault(item['shipment'], []).append(item)
            order['shipments'] = []
            for shipment, same in sorted(shipments.items()):
                order['shipments'].append({'number': shipment,
                    'quantity': unique([i['quantity_shipped'] for i in same], 'shipment_quantity'),
                    'date': unique([i['shipment_date'] for i in same], 'shipment_date')})
            order['quantity_shipped'] = sum(s['quantity'] for s in order['shipments'])
            if order['quantity_ordered'] is not None and order['quantity_shipped'] > order['quantity_ordered']:
                raise OrderEmailError('shipped_exceeds_ordered')
            order['tracking'] = sorted({t for i in items for t in i['tracking']})
            order['invoices'] = sorted({i['invoice_notification'] for i in items if i['invoice_notification']})
            order['sources'] = sorted({f"https://mail.google.com/mail/u/?authuser=linjhumse%40gmail.com#all/{d['thread_id']}" for d in docs})
            order['message_ids'] = sorted({d['message_id'] for d in docs})
            order['unit_price'] = unique([i['unit_price'] for i in items], 'unit_price')
            merged[key] = order
        except OrderEmailError as error:
            conflicts[key] = str(error)
    return merged, conflicts


def order_row(order):
    notes = ['PO: ' + order['purchase_order']] if order.get('purchase_order') else []
    for shipment in order['shipments']:
        notes.append(f"Shipment {shipment['number']}: {shipment['quantity']} {order['unit']}(s), {shipment['date'] or 'date unknown'}")
    if order['invoices']:
        notes.append('Invoice notification ' + ', '.join(order['invoices']) + '; not a bill to pay')
    if order['unit_price']:
        currency = (order.get('currency') or 'currency unknown') if order.get('semantic_version') else 'USD'
        notes.append('Unit price: ' + currency + ' ' + order['unit_price'])
    notes.append('See Package receipts for received quantities; full shipment receipt not confirmed')
    shipped, ordered = order['quantity_shipped'], order['quantity_ordered']
    progress = f'Shipped {shipped}' + (f' of {ordered}' if ordered is not None else '; ordered total unknown')
    if order.get('semantic_version'):
        from order_reconcile import progress as status_progress
        progress = status_progress(order)
        if order.get('status_detail'): notes.append('Email status detail: '+order['status_detail'])
        if order.get('invoice_numbers'): notes.append('Invoice: ' + ', '.join(order['invoice_numbers']) + '; payment status not inferred')
        if order.get('shipment_total_unknown'): notes.append('Shipment quantity cannot be totaled without distinct shipment identifiers.')
    spec = '; '.join(x for x in (order['specifications'], order['pack_size']) if x)
    return [order['order_id'], (date.fromisoformat(order['order_date']) - EPOCH).days if order.get('order_date') else '',
            order['requester'] or '', order['supplier'], order['product'], order['catalog'], spec,
            ordered if ordered is not None else '', order['unit'] or '', progress,
            ', '.join(order['tracking']), '\n'.join(order['sources']), '; '.join(notes)]


def padded(row, width=13):
    return row + [''] * (width - len(row))


class GoogleOrderStore(GoogleReceiptStore):
    def __init__(self, config, session_factory=None):
        super().__init__(config, session_factory)
        self.sheet_id = config['tabs']['Orders']['sheet_id']

    def values(self, address):
        return self.request('GET', self.base + '/values/' + quote("'Orders'!" + address, safe=''),
                            params={'valueRenderOption': 'UNFORMATTED_VALUE'}).get('values', [])

    def snapshot(self):
        meta = self.request('GET', self.base, params={'fields': 'sheets(properties)'})
        sheet = next((s['properties'] for s in meta['sheets'] if s['properties']['sheetId'] == self.sheet_id), None)
        if not sheet or sheet['title'] != 'Orders' or sheet['gridProperties']['columnCount'] < 13:
            raise SheetSyncError('orders_tab_changed')
        self.row_count = sheet['gridProperties']['rowCount']
        if self.values('A4:M4') != [HEADERS]:
            raise SheetSyncError('orders_headers_changed')
        rows = {}
        for start in range(5, self.row_count + 1, 1000):
            rows.update({start + i: padded(r) for i, r in enumerate(self.values(f'A{start}:M{min(start + 999, self.row_count)}')) if any(v != '' for v in r)})
        return rows

    def read_row(self, row):
        return padded(next(iter(self.values(f'A{row}:M{row}')), [])) if row <= self.row_count else [''] * 13

    def write_row(self, row, values, expected):
        if row > self.row_count:
            self.request('POST', self.base + ':batchUpdate', json={'requests': [{'updateSheetProperties': {
                'properties': {'sheetId': self.sheet_id, 'gridProperties': {'rowCount': row + 99}}, 'fields': 'gridProperties.rowCount'}}]})
            self.row_count = row + 99
        cells = self.request('GET', self.base, params={'ranges': f"'Orders'!A{row}:M{row}",
            'includeGridData': 'true', 'fields': 'sheets(data(rowData(values(userEnteredValue,effectiveValue,dataValidation,chipRuns))))'})
        actual = []
        for sheet in cells.get('sheets', []):
            for data in sheet.get('data', []):
                for line in data.get('rowData', []):
                    for cell in line.get('values', []):
                        value = cell.get('userEnteredValue', {})
                        if 'formulaValue' in value or cell.get('dataValidation') or cell.get('chipRuns'):
                            raise SheetSyncError('orders_row_structure_changed')
                        actual.append(value.get('stringValue', value.get('numberValue', value.get('boolValue', ''))))
        if padded(actual) != expected:
            raise SheetSyncError('orders_row_changed')
        bounds = {'sheetId': self.sheet_id, 'startRowIndex': row - 1, 'endRowIndex': row, 'startColumnIndex': 0, 'endColumnIndex': 13}
        req = [{'updateCells': {'range': bounds, 'rows': [{'values': [{'userEnteredValue':
                  {'numberValue': v} if isinstance(v, (int, float)) else {'stringValue': v}} for v in values]}], 'fields': 'userEnteredValue'}},
               {'repeatCell': {'range': bounds, 'cell': {'userEnteredFormat': {'wrapStrategy': 'WRAP', 'verticalAlignment': 'TOP'}},
                               'fields': 'userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment'}},
               {'repeatCell': {'range': {**bounds, 'startColumnIndex': 1, 'endColumnIndex': 2},
                  'cell': {'userEnteredFormat': {'numberFormat': {'type': 'DATE', 'pattern': 'yyyy-mm-dd'}}}, 'fields': 'userEnteredFormat.numberFormat'}},
               {'repeatCell': {'range': {**bounds, 'startColumnIndex': 7, 'endColumnIndex': 8},
                  'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '0'}}}, 'fields': 'userEnteredFormat.numberFormat'}},
               {'updateDimensionProperties': {'range': {'sheetId': self.sheet_id, 'dimension': 'ROWS', 'startIndex': row - 1, 'endIndex': row},
                  'properties': {'pixelSize': 140}, 'fields': 'pixelSize'}}]
        if row > 104:
            req.append({'setBasicFilter': {'filter': {'range': {'sheetId': self.sheet_id,
                'startRowIndex': 3, 'endRowIndex': self.row_count, 'startColumnIndex': 0, 'endColumnIndex': 13}}}})
        self.request('POST', self.base + ':batchUpdate', json={'requests': req})


def sync_orders(orders, store, journal_path):
    journal = json.loads(journal_path.read_text(encoding='utf-8')) if journal_path.exists() else {'spreadsheet_id': store.config['spreadsheet_id'], 'items': {}}
    if journal['spreadsheet_id'] != store.config['spreadsheet_id']:
        raise SheetSyncError('order_destination_changed')
    remote = store.snapshot()
    by_key = {}
    for row, values in remote.items():
        if values[0] and values[3] and values[5]:
            key = order_key(values[3], values[0], values[5])
            if key in by_key:
                raise SheetSyncError('duplicate_order_rows')
            by_key[key] = row
    statuses = {}
    for key, order in orders.items():
        desired = order_row(order)
        state = journal['items'].setdefault(key, {})
        row = by_key.get(key, state.get('pending_row'))
        if row is None:
            reserved = [s['pending_row'] for s in journal['items'].values() if s.get('pending_row')]
            row = max([4, *remote, *reserved]) + 1
        actual = remote.get(row, [''] * 13)
        # Reconcile a lost response before attempting any repeat write.
        if actual == state.get('pending_values'):
            state['last_values'] = actual
            state.pop('pending_values', None)
            state.pop('pending_row', None)
        if actual == desired:
            state.update(last_values=desired, row=row, status='synced')
            statuses[key] = 'synced'
            continue
        expected = state.get('last_values', [''] * 13)
        if actual != expected:
            state.update(status='needs_review', error='order_row_manually_changed')
            statuses[key] = 'needs_review'
            continue
        state.update(pending_row=row, pending_values=desired, status='writing')
        write_json(journal_path, journal)
        store.write_row(row, desired, expected)
        if store.read_row(row) != desired:
            raise SheetSyncError('order_readback_mismatch')
        state.update(last_values=desired, row=row, status='synced')
        state.pop('pending_row', None)
        state.pop('pending_values', None)
        remote[row] = desired
        by_key[key] = row
        statuses[key] = 'synced'
        write_json(journal_path, journal)
    write_json(journal_path, journal)
    return statuses


def match_receipt(receipt, orders):
    from product_matching import receipt_order_match
    return receipt_order_match(receipt, orders)


def sync_matches(orders, store, state_path):
    rows = store.snapshot()
    matches = {}
    for row, receipt in rows.items():
        receipt = padded(receipt, 18)
        match = match_receipt(receipt, orders)
        matches[receipt[0]] = match
        if not match['candidates']:
            continue
        quantity_note = 'received quantity recorded separately' if receipt[8] != '' else 'receipt quantity remains unconfirmed'
        addition = ('Order match: ' + match['confirmed_order'] + '; ' + quantity_note) if match['confirmed_order'] else (
            'Possible order: ' + ', '.join(match['candidates']) + ' (catalog match only; not confirmed)')
        if addition in receipt[17]:
            continue
        # Read the exact notes/order cells immediately before a narrow update.
        meta = store.request('GET', store.base, params={'ranges': f"'Package receipts'!P{row}:R{row}", 'includeGridData': 'true',
            'fields': 'sheets(data(rowData(values(userEnteredValue,dataValidation,chipRuns))))'})
        cells = next(iter(next(iter(meta.get('sheets', [{}]))).get('data', [{}])), {}).get('rowData', [{}])[0].get('values', [])
        if any(c.get('dataValidation') or c.get('chipRuns') or 'formulaValue' in c.get('userEnteredValue', {}) for c in cells):
            continue
        current = store.read_row(row)
        if padded(current, 18) != receipt:
            continue
        requests = [{'updateCells': {'range': {'sheetId': store.sheet_id, 'startRowIndex': row - 1, 'endRowIndex': row,
            'startColumnIndex': 17, 'endColumnIndex': 18}, 'rows': [{'values': [{'userEnteredValue': {'stringValue': receipt[17] + '; ' + addition}}]}],
            'fields': 'userEnteredValue'}}]
        if match['confirmed_order'] and not receipt[15]:
            requests.append({'updateCells': {'range': {'sheetId': store.sheet_id, 'startRowIndex': row - 1, 'endRowIndex': row,
                'startColumnIndex': 15, 'endColumnIndex': 16}, 'rows': [{'values': [{'userEnteredValue': {'stringValue': match['confirmed_order']}}]}],
                'fields': 'userEnteredValue'}})
        store.request('POST', store.base + ':batchUpdate', json={'requests': requests})
        if addition not in padded(store.read_row(row), 18)[17]:
            raise SheetSyncError('receipt_match_readback_failed')
    write_json(state_path, matches)
    return matches


class OrderWorker:
    def __init__(self, config, sheet_config, notifier=None):
        self.config, self.sheet_config = config, sheet_config
        self.root = ROOT / '.local' / 'email-intake'
        self.store = GoogleOrderStore(sheet_config)
        self.receipts = GoogleReceiptStore(sheet_config)
        self.stop = Event()
        self.notifier = notifier

    def run_once(self):
        lock = InstanceLock(ROOT / '.local' / 'order-sync.lock')
        lock.acquire()
        try:
            GmailOrderIntake(self.root, self.config, self.store).capture()
            parsed = parse_pending(self.root)
            orders, conflicts = merge_documents([d for r in parsed if r['status'] == 'parsed' for d in parsed_documents(r)])
            write_json(self.root / 'orders.json', {'orders': orders, 'conflicts': conflicts})
            receipt_lock = InstanceLock(ROOT / '.local' / 'sheet-sync.lock')
            receipt_lock.acquire(timeout=45)
            try:
                statuses = sync_orders(orders, self.store, self.root / 'sheet-sync.json') if orders else {}
                matches = sync_matches({k: v for k, v in orders.items() if statuses.get(k) == 'synced'}, self.receipts, self.root / 'receipt-matches.json')
            finally:
                receipt_lock.release()
            report = {'status': 'ok', 'checked_at': time.time(), 'parsed_messages': sum(r['status'] == 'parsed' for r in parsed),
                      'queued_messages': sum(r['status'] in ('waiting_usage','retry_wait') for r in parsed),
                      'needs_review': sum(r['status'] == 'needs_review' for r in parsed) + len(conflicts) + sum(s == 'needs_review' for s in statuses.values()),
                      'order_lines': len(orders), 'synced_lines': sum(s == 'synced' for s in statuses.values()),
                      'confirmed_receipt_matches': sum(bool(m['confirmed_order']) for m in matches.values())}
            if self.notifier:
                from order_notifications import review_messages
                report['slack_notifications'] = self.notifier.run(orders,statuses,self.store,review_messages(self.root,conflicts))
            write_json(ROOT / '.local' / 'email-worker-status.json', report)
            return report
        finally:
            lock.release()

    def run(self):
        while not self.stop.is_set():
            try:
                report = self.run_once()
                LOG.info('Order email pass synced=%s review=%s', report['synced_lines'], report['needs_review'])
            except Exception as error:
                code = str(error) if isinstance(error, (OrderEmailError, SheetSyncError)) else type(error).__name__
                write_json(ROOT / '.local' / 'email-worker-status.json', {'status': 'retry_wait', 'error': code, 'checked_at': time.time()})
                LOG.warning('Order email pass queued: %s', code)
            self.stop.wait(self.config.get('poll_seconds', 300))

    def start(self):
        self.thread = Thread(target=self.run, name='order-email-sync', daemon=True)
        self.thread.start()


def configured_order_worker(client=None, channel_id=None):
    path = ROOT / '.local' / 'email-config.json'
    config = json.loads(path.read_text()) if path.exists() else {}
    if not config.get('enabled'):
        return None
    sheet_config=json.loads((ROOT / '.local' / 'google-sheet.json').read_text())
    notifier=None
    if config.get('slack_notifications_enabled'):
        from order_notifications import OrderNotifier
        if client is None or channel_id is None:
            from slack_bot import Settings
            from slack_sdk import WebClient
            settings=Settings.from_environment()
            client=WebClient(token=settings.bot_token)
            identity=client.auth_test()
            if identity.get('team_id')!=settings.team_id or not identity.get('bot_id'):
                raise ValueError('wrong_order_notification_workspace')
            channel_id=settings.channel_id
        notifier=OrderNotifier(ROOT/'.local'/'email-intake'/'slack-notifications.json',client,channel_id,sheet_config)
    return OrderWorker(config,sheet_config,notifier)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', required=True)
    parser.parse_args()
    worker = configured_order_worker()
    if not worker:
        raise SystemExit('Enable the local email configuration first.')
    print(json.dumps(worker.run_once()))
