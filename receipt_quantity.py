"""Infer receipt contents under the lab's photograph-each-received-package rule."""

import hashlib
import json
from pathlib import Path
import re

from message_intent import is_delivery_item, legacy_shortage
from pack_contents import total as semantic_contents


def receipt_id(record, index):
    return 'R-' + hashlib.sha256(f"{record['channel_id']}:{record['message_ts']}:{index}".encode()).hexdigest()[:20]


def normalized(value):
    return re.sub(r'[^A-Z0-9]', '', (value or '').upper())


def contents_per_package(text, unit):
    text = (text or '').upper()
    if unit == 'case':
        packs = re.search(r'\b(\d+)\s*PK\s*/\s*CS\b', text)
        each = re.search(r'\b(\d+)\s*EA\s*/\s*PK\b', text)
        if packs and each:
            return int(packs[1]) * int(each[1])
        direct = re.search(r'\b(\d+)\s*EA\s*/\s*CS\b|\bCASE\s+OF\s+(\d+)\b', text)
    elif unit in ('carton','box'):
        direct = re.search(r'\b'+unit.upper()+r'\s+OF\s+(\d+)\b', text)
    elif unit == 'pack':
        direct = re.search(r'\b(\d+)\s*EA\s*/\s*PK\b|\bPACK\s+OF\s+(\d+)\b', text)
    else:
        return None
    return int(next(g for g in direct.groups() if g is not None)) if direct else None


def infer_quantity(item, orders=(), caption=None):
    if not is_delivery_item(item):
        return None
    observation = item.get('package_observation') or {}
    stated = item.get('stated_quantity')
    if item.get('confidence') == 'low' or item.get('label_type') != 'product':
        return None
    if stated is not None:
        if (stated.get('scope') != 'this_item' or not stated.get('quote') or
                (caption is not None and stated['quote'] not in caption)):
            return None
        count, unit = stated.get('count'), stated.get('unit')
        if unit == 'package' and observation.get('package_unit') in ('case', 'pack', 'carton', 'box') and observation.get('evidence'):
            unit = observation['package_unit']
    else:
        count, unit = observation.get('distinct_packages'), observation.get('package_unit')
        if not observation.get('evidence'):
            return None
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return None
    def unconverted():
        if not stated or unit not in ('case', 'pack', 'carton', 'box', 'package', 'each'):
            return None
        return {'quantity': count, 'unit': unit, 'packages': count, 'package_unit': unit,
                'per_package': None, 'basis': 'member-stated receipt count',
                'note': f'Reported received: {count} {unit}(s); member statement: {stated["quote"]}; '
                        + ('individual count stated directly.' if unit == 'each' else 'contents per package not established; no item-total conversion.')}
    # A carton may contain multiple cases or a mixture: do not divide shipment
    # totals by carton counts or equate an unidentified carton with a sale unit.
    if unit == 'each' and not stated:
        return {'quantity':count,'unit':'each','packages':count,'package_unit':'each',
                'per_package':1,'basis':'visible received individual units',
                'note':f'Inferred received: {count} each; clearly identified individual received units.'}
    if unit not in ('case', 'pack', 'carton', 'box'):
        return unconverted()
    literal = contents_per_package(item.get('packaging_text'), unit)
    interpreted = semantic_contents(item.get('pack_contents'), unit)
    if literal is not None and interpreted is not None and literal != interpreted:
        return None
    per_label = interpreted or literal
    candidates = [o for o in orders if normalized(o.get('catalog')) == normalized(item.get('catalog_number'))
                  and normalized(o.get('supplier')) in normalized(item.get('brand_or_supplier'))
                  and o.get('unit') == unit]
    per_order = {contents_per_package(o.get('pack_size'), unit) for o in candidates}
    per_order.discard(None)
    if len(per_order) > 1 or (per_label is not None and per_order and per_label not in per_order):
        return None
    per_package = per_label or next(iter(per_order), None)
    if not per_package or per_package < 1:
        return unconverted()
    sources = 'package label' if per_label else 'matching supplier shipment pack size'
    if per_label and per_order:
        sources += ' and matching shipment pack size'
    count_basis = f'member-stated total: {stated["quote"]}' if stated else 'lab photographs received packages'
    return {'quantity': count * per_package, 'unit': 'each', 'packages': count, 'package_unit': unit,
            'per_package': per_package, 'basis': sources,
            'note': f'Inferred received: {count} {unit}(s) x {per_package} each = {count * per_package} each; '
                    f'{count_basis}; basis: {sources}'}


def quantity_candidates(root, orders=()):
    """Stable message order prevents a reupload of identical image bytes adding stock."""
    root = Path(root)
    overrides_path = root.parent / 'package-observations.json'
    overrides = json.loads(overrides_path.read_text(encoding='utf-8')) if overrides_path.exists() else {}
    seen, output = set(), {}
    for path in sorted(root.glob('*/record.json')):
        analysis_path = path.with_name('analysis.json')
        if not analysis_path.exists():
            continue
        record = json.loads(path.read_text(encoding='utf-8'))
        analysis = json.loads(analysis_path.read_text(encoding='utf-8'))
        if legacy_shortage(record, analysis.get('result', {})): continue
        files = {f['file_id']: f for f in record['files'] if f.get('status') == 'downloaded'}
        for index, original in enumerate(analysis.get('result', {}).get('fields', {}).get('items', [])):
            rid = receipt_id(record, index)
            item = dict(original)
            if rid in overrides:
                item['package_observation'] = overrides[rid]
            source = [files[f] for f in item['source_file_ids'] if f in files]
            hashes = {(normalized(item.get('brand_or_supplier')), normalized(item.get('catalog_number')), f['sha256'])
                      for f in source if f.get('sha256')}
            # The same files used for multiple labels/items must not be counted
            # twice; mixed-item cartons remain unknown under this initial rule.
            if not hashes or hashes & seen:
                continue
            if any(set(item['source_file_ids']) & set(other['source_file_ids']) and
                   not ((item.get('stated_quantity') or {}).get('scope') == 'this_item' and
                        (other.get('stated_quantity') or {}).get('scope') == 'this_item' and
                        normalized(item.get('catalog_number')) and normalized(other.get('catalog_number')) and
                        normalized(item.get('catalog_number')) != normalized(other.get('catalog_number')))
                   for j, other in enumerate(analysis.get('result', {}).get('fields', {}).get('items', []))
                   if j != index and other['label_type'] == 'product' and is_delivery_item(other)):
                continue
            inferred = infer_quantity(item, orders, record.get('caption', ''))
            if inferred:
                output[rid] = inferred
                seen.update(hashes)
    return output


def sync_quantities(root, store, orders=()):
    candidates = quantity_candidates(root, orders)
    if not candidates:
        return 0
    rows = store.snapshot()
    applied = 0
    for row, original in rows.items():
        values = original + [''] * (18 - len(original))
        inferred = candidates.get(values[0])
        if not inferred or values[8] != '' or values[9] != '':
            continue  # Preserve prior writes and operator corrections.
        meta = store.request('GET', store.base, params={'ranges': f"'Package receipts'!I{row}:R{row}",
            'includeGridData': 'true', 'fields': 'sheets(data(rowData(values(userEnteredValue,dataValidation,chipRuns))))'})
        for sheet in meta.get('sheets', []):
            for data in sheet.get('data', []):
                for line in data.get('rowData', []):
                    for index, cell in enumerate(line.get('values', [])):
                        if index in (0, 1, 9) and (cell.get('dataValidation') or cell.get('chipRuns') or
                                'formulaValue' in cell.get('userEnteredValue', {})):
                            raise ValueError('receipt_quantity_cell_structure_changed')
        actual = store.read_row(row)
        if actual + [''] * (18 - len(actual)) != values:
            continue
        notes = values[17] + '; ' + inferred['note']
        def update(col, cells):
            return {'updateCells': {'range': {'sheetId': store.sheet_id, 'startRowIndex': row - 1, 'endRowIndex': row,
                'startColumnIndex': col, 'endColumnIndex': col + len(cells)},
                'rows': [{'values': [{'userEnteredValue': {'numberValue': v} if isinstance(v, int) else {'stringValue': v}} for v in cells]}],
                'fields': 'userEnteredValue'}}
        req = [update(8, [inferred['quantity'], inferred['unit']]), update(17, [notes]),
               {'repeatCell': {'range': {'sheetId': store.sheet_id, 'startRowIndex': row - 1, 'endRowIndex': row,
                   'startColumnIndex': 8, 'endColumnIndex': 9}, 'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '0'}}},
                   'fields': 'userEnteredFormat.numberFormat'}},
               {'autoResizeDimensions': {'dimensions': {'sheetId': store.sheet_id, 'dimension': 'ROWS', 'startIndex': row - 1, 'endIndex': row}}}]
        store.request('POST', store.base + ':batchUpdate', json={'requests': req})
        readback = store.read_row(row)
        if readback[8:10] != [inferred['quantity'], inferred['unit']] or readback[17] != notes:
            raise ValueError('receipt_quantity_readback_failed')
        applied += 1
    return applied
