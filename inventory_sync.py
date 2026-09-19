"""Rebuild delivery totals from live source rows, without incrementing stock."""

import hashlib
import json
import math
import time
from pathlib import Path
from urllib.parse import quote

from photo_intake import write_json
from receipt_quantity import contents_per_package, normalized, receipt_id
from sheet_sync import GoogleReceiptStore, SheetSyncError

HEADERS = ['Item ID', 'Product', 'Supplier / brand', 'Catalog number',
           'Ordered', 'Received', 'Outstanding', 'Unit', 'Reconciliation',
           'Confirmed storage', 'Source links', 'Notes']
GUIDANCE = 'Delivery totals from tracked orders and receipts. Provisional matches are marked; usage and opening stock are not tracked.'


def pad(row, width):
    return list(row) + [''] * max(0, width - len(row))


def supplier(value):
    value = normalized(value)
    return 'FISHERSCIENTIFIC' if 'FISHERSCIENTIFIC' in value else value


def each_quantity(quantity, unit, pack):
    if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not math.isfinite(quantity) or quantity < 0:
        return None
    unit = str(unit).strip().lower()
    factor = 1 if unit in ('each', 'ea', 'bottle', 'vial', 'tube', 'piece', 'item') else contents_per_package(pack, unit)
    return quantity * factor if factor else None


def source_hashes(root):
    result = {}
    for path in sorted(Path(root).glob('*/record.json')):
        analysis = path.with_name('analysis.json')
        if not analysis.exists():
            continue
        record = json.loads(path.read_text(encoding='utf-8'))
        data = json.loads(analysis.read_text(encoding='utf-8'))
        files = {f['file_id']: f.get('sha256') for f in record['files']}
        for index, item in enumerate(data.get('result', {}).get('fields', {}).get('items', [])):
            result[receipt_id(record, index)] = {files[f] for f in item['source_file_ids'] if files.get(f)}
    return result


def build_rows(orders, receipts, hashes=None):
    """Group by supplier/catalog; never allocate a catalog-only receipt to an order."""
    groups, seen_orders, seen_receipts, seen_sources = {}, set(), set(), set()
    hashes = hashes or {}

    def group(brand, catalog, product):
        key = (supplier(brand), normalized(catalog))
        if not all(key):
            raise SheetSyncError('inventory_source_identity_missing')
        return groups.setdefault(key, {'brand': brand, 'catalog': catalog, 'product': product,
                                      'orders': [], 'receipts': [], 'duplicates': 0})

    for original in orders:
        r = pad(original, 13)
        key = (supplier(r[3]), normalized(r[5]), normalized(r[0]))
        if key in seen_orders:
            raise SheetSyncError('inventory_duplicate_order_rows')
        seen_orders.add(key)
        g = group(r[3], r[5], r[4])
        g['orders'].append(r)
    for original in sorted(receipts, key=lambda r: (str(r[1]), str(r[0]))):
        r = pad(original, 18)
        if not r[0] or r[0] in seen_receipts:
            raise SheetSyncError('inventory_duplicate_receipt_ids')
        seen_receipts.add(r[0])
        # Carrier-only observations are not a separate product receipt.
        if not r[5] or not r[4]:
            continue
        g = group(r[4], r[5], r[3])
        keys = {('hash', supplier(r[4]), normalized(r[5]), h) for h in hashes.get(r[0], ())}
        if r[16]:
            keys.add(('link', supplier(r[4]), normalized(r[5]), r[16]))
        if keys & seen_sources:
            g['duplicates'] += 1
            continue
        # Unknown quantities do not suppress a later quantified view.
        if each_quantity(r[8], r[9], r[7]) is not None:
            seen_sources.update(keys)
        g['receipts'].append(r)

    output = {}
    for key, g in sorted(groups.items()):
        os, rs = g['orders'], g['receipts']
        oq = [each_quantity(r[7], r[8], r[6]) for r in os]
        rq = [each_quantity(r[8], r[9], r[7]) for r in rs]
        ordered = sum(oq) if os and None not in oq else ''
        received = sum(q for q in rq if q is not None)
        unknown = sum(q is None for q in rq)
        outstanding = max(0, ordered - received) if ordered != '' and not unknown else ''
        confirmed = []
        for r in rs:
            hits = [o for o in os if (r[15] and normalized(r[15]) == normalized(o[0])) or
                    (r[14] and normalized(r[14]) in {normalized(t.strip()) for t in o[10].split(',') if t.strip()}
                     and not r[15])]
            confirmed.append(hits[0][0] if len(hits) == 1 else None)
        status = 'Matched' if rs and all(confirmed) else ('Awaiting receipts' if not rs else 'Provisional order match')
        notes = ['Cumulative receipts; does not subtract supplies used.']
        if not os:
            status = 'No matching order'
        if None in oq:
            status = 'Ordered quantity unknown'
        if unknown:
            status = 'Receipt quantity incomplete'
            notes.append(f'{unknown} receipt(s) have unknown quantity/unit; Received is the known subtotal.')
        if rs and not all(confirmed) and os:
            notes.append('Possible order(s): ' + ', '.join(o[0] for o in os) + '; outstanding is provisional.')
        elif os:
            notes.append('Order(s): ' + ', '.join(o[0] for o in os))
        if ordered != '' and received > ordered:
            status = 'Received exceeds ordered'
            notes.append(f'Excess received: {received - ordered} each; check order coverage.')
        # Detect an over-receipt on one exact order even if another offsets it.
        for o, total in zip(os, oq):
            exact_total = sum(q or 0 for q, match in zip(rq, confirmed) if match == o[0])
            if total is not None and exact_total > total:
                status = 'Order allocation needs review'
        if any('cancelled' in str(o[9]).lower() or 'canceled' in str(o[9]).lower() for o in os):
            outstanding = ''
            status = 'Cancelled order included; outstanding unknown'
            notes.append('Original ordered amounts are retained; cancellation quantities are not inferred.')
        if any('Inferred received:' in r[17] for r in rs):
            notes.append('Received includes quantities inferred from photographed packages; see receipt calculations.')
        if g['duplicates']:
            notes.append(f"Excluded {g['duplicates']} repeated source receipt(s).")
        locations = sorted({str(r[11]) for r in rs if r[11] != ''})
        sources = sorted({link for o in os for link in o[11].splitlines() if link} |
                         {r[16] for r in rs if r[16]})
        item_id = 'I-' + hashlib.sha256('|'.join(key).encode()).hexdigest()[:16]
        output[item_id] = [item_id, g['product'], g['brand'], g['catalog'], ordered, received,
                           outstanding, 'each', status, ', '.join(locations), '\n'.join(sources), ' '.join(notes)]
    return output


class GoogleInventoryStore(GoogleReceiptStore):
    def __init__(self, config, session_factory=None):
        super().__init__(config, session_factory)
        self.sheet_id = config['tabs']['Inventory']['sheet_id']

    def values(self, address):
        return self.request('GET', self.base + '/values/' + quote("'Inventory'!" + address, safe=''),
                            params={'valueRenderOption': 'UNFORMATTED_VALUE'}).get('values', [])

    def snapshot(self):
        meta = self.request('GET', self.base, params={'fields': 'sheets(properties)'})
        tab = next((s['properties'] for s in meta['sheets'] if s['properties']['sheetId'] == self.sheet_id), None)
        if not tab or tab['title'] != 'Inventory' or tab['gridProperties']['columnCount'] < 12:
            raise SheetSyncError('inventory_tab_changed')
        self.row_count = tab['gridProperties']['rowCount']
        if self.values('A4:L4') != [HEADERS]:
            raise SheetSyncError('inventory_headers_changed')
        rows = {}
        for start in range(5, self.row_count + 1, 1000):
            rows.update({start+i: pad(r, 12) for i, r in enumerate(self.values(f'A{start}:L{min(start+999,self.row_count)}')) if any(v != '' for v in r)})
        return rows

    def read_row(self, row):
        return pad(next(iter(self.values(f'A{row}:L{row}')), []), 12)

    def write_row(self, row, values, expected):
        if row > self.row_count:
            self.request('POST', self.base + ':batchUpdate', json={'requests': [{'updateSheetProperties': {
                'properties': {'sheetId': self.sheet_id, 'gridProperties': {'rowCount': row + 99}}, 'fields': 'gridProperties.rowCount'}}]})
            self.row_count = row + 99
        meta = self.request('GET', self.base, params={'ranges': f"'Inventory'!A{row}:L{row}", 'includeGridData': 'true',
            'fields': 'sheets(data(rowData(values(userEnteredValue,dataValidation,chipRuns))))'})
        actual = []
        for sheet in meta.get('sheets', []):
            for data in sheet.get('data', []):
                for line in data.get('rowData', []):
                    for cell in line.get('values', []):
                        v = cell.get('userEnteredValue', {})
                        if 'formulaValue' in v or cell.get('dataValidation') or cell.get('chipRuns'):
                            raise SheetSyncError('inventory_row_structure_changed')
                        actual.append(next(iter(v.values()), ''))
        if pad(actual, 12) != expected:
            raise SheetSyncError('inventory_row_changed')
        bounds = {'sheetId': self.sheet_id, 'startRowIndex': row-1, 'endRowIndex': row, 'startColumnIndex': 0, 'endColumnIndex': 12}
        req = [{'updateCells': {'range': bounds, 'rows': [{'values': [{'userEnteredValue':
                    {'numberValue': v} if isinstance(v, (int, float)) else {'stringValue': v}} for v in values]}], 'fields': 'userEnteredValue'}},
               {'repeatCell': {'range': bounds, 'cell': {'userEnteredFormat': {'wrapStrategy': 'WRAP', 'verticalAlignment': 'TOP',
                    'numberFormat': {'type': 'TEXT'}}}, 'fields': 'userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment,userEnteredFormat.numberFormat'}},
               {'updateCells': {'range': {**bounds, 'startColumnIndex': 4, 'endColumnIndex': 7},
                    'rows': [{'values': [{'userEnteredFormat': {'numberFormat': {'type': 'NUMBER',
                        'pattern': '#,##0.0########' if isinstance(v, float) and not v.is_integer() else '#,##0'}}}
                        for v in values[4:7]]}], 'fields': 'userEnteredFormat.numberFormat'}},
               {'autoResizeDimensions': {'dimensions': {'sheetId': self.sheet_id, 'dimension': 'ROWS', 'startIndex': row-1, 'endIndex': row}}}]
        if row > 104:
            req.append({'setBasicFilter': {'filter': {'range': {**bounds, 'startRowIndex': 3, 'endRowIndex': self.row_count}}}})
        self.request('POST', self.base + ':batchUpdate', json={'requests': req})


def sync_rows(desired, store, path):
    journal = json.loads(path.read_text()) if path.exists() else {'spreadsheet_id': store.config['spreadsheet_id'], 'items': {}}
    if journal['spreadsheet_id'] != store.config['spreadsheet_id']:
        raise SheetSyncError('inventory_destination_changed')
    remote, by_id = store.snapshot(), {}
    for row, values in remote.items():
        if values[0] in by_id:
            raise SheetSyncError('inventory_duplicate_item_ids')
        by_id[values[0]] = row
    # Reconcile vanished sources to an explicitly empty delivery total instead of stale stock.
    for key, state in journal['items'].items():
        if key not in desired and state.get('last_values'):
            old = list(state['last_values'])
            old[4:7] = ['', 0, '']; old[8] = 'Source records removed'; old[10] = ''
            old[11] = 'No current source records. Check Orders and Package receipts.'
            desired[key] = old
    statuses = {}
    for key, values in desired.items():
        state = journal['items'].setdefault(key, {})
        row = by_id.get(key, state.get('pending_row'))
        if row is None:
            row = max([4, *remote, *(s['pending_row'] for s in journal['items'].values() if s.get('pending_row'))]) + 1
        actual = remote.get(row, [''] * 12)
        if actual == state.get('pending_values'):
            state['last_values'] = actual
        if actual != values:
            expected = state.get('last_values', [''] * 12)
            if actual != expected:
                statuses[key] = 'needs_review'
                continue
            state.update(pending_row=row, pending_values=values)
            write_json(path, journal)
            store.write_row(row, values, expected)
            if store.read_row(row) != values:
                raise SheetSyncError('inventory_readback_mismatch')
        state.update(last_values=values, row=row)
        state.pop('pending_row', None); state.pop('pending_values', None)
        remote[row] = values
        statuses[key] = 'synced'
        write_json(path, journal)
    write_json(path, journal)
    return statuses


def sync_inventory(root, receipts):
    from order_sync import GoogleOrderStore
    root = Path(root)
    status_path = root.parent / 'inventory-worker-status.json'
    try:
        orders = GoogleOrderStore(receipts.config)
        target = GoogleInventoryStore(receipts.config)
        desired = build_rows(orders.snapshot().values(), receipts.snapshot().values(), source_hashes(root))
        states = sync_rows(desired, target, root.parent / 'inventory-sync.json')
        report = {'status': 'needs_review' if 'needs_review' in states.values() else 'ok',
                  'checked_at': time.time(), 'items': len(states), 'needs_review': sum(v == 'needs_review' for v in states.values())}
        write_json(status_path, report)
        return report
    except Exception as error:
        write_json(status_path, {'status': 'retry_wait', 'checked_at': time.time(),
                   'error': str(error) if isinstance(error, SheetSyncError) else type(error).__name__})
        raise
