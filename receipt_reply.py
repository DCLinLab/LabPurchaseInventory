"""Prepare a receipt reply from verified live Orders and Inventory rows."""

import re
from pathlib import Path
from urllib.parse import quote

from inventory_sync import GoogleInventoryStore, sync_inventory
from label_reader import plain
from message_intent import is_delivery_item
from order_sync import GoogleOrderStore, order_key, sync_matches
from product_matching import same_product, receipt_order_match
from receipt_quantity import infer_quantity, receipt_id, sync_quantities
from runtime_lock import InstanceLock
from sheet_sync import GoogleReceiptStore, ReceiptSync, SheetSyncError


def live_orders(rows):
    orders = {}
    for row, r in rows.items():
        key = order_key(r[3],r[0],r[5])
        if key in orders:
            raise SheetSyncError('duplicate_order_rows')
        po = re.search(r'(?:^|;\s*)PO:\s*([^;]+)',r[12])
        orders[key] = {'order_id':r[0], 'purchase_order':po[1].strip() if po else None,
                      'supplier':r[3], 'product':r[4], 'catalog':r[5],
                      'specifications':r[6], 'pack_size':r[6], 'quantity_ordered':r[7],
                      'unit':r[8], 'tracking':[t.strip() for t in r[10].split(',') if t.strip()],
                      'row':row, 'requester':r[2]}
    return orders


def row_link(config, tab, row):
    return ('https://docs.google.com/spreadsheets/d/' + config['spreadsheet_id'] + '/edit#gid=' +
            str(config['tabs'][tab]['sheet_id']) + '&range=' + quote(f'A{row}'))


def build_reply(result, record, orders, inventory, receipts, config, preview=False):
    lines, links, contexts = [], [], []
    by_id = {r[0]: (row,r) for row,r in receipts.items() if r and r[0]}
    for index,item in enumerate(result['fields']['items']):
        if not is_delivery_item(item):
            continue
        rid = receipt_id(record,index)
        hit = by_id.get(rid)
        if not hit:
            raise SheetSyncError('reply_receipt_not_synced')
        receipt_row, receipt = hit
        r = receipt + [''] * max(0,18-len(receipt))
        match = receipt_order_match(r,orders)
        choices = [o for o in orders.values() if o['order_id'] in match['candidates'] and
                   same_product(r[4],r[5],r[6],o['supplier'],o['catalog'],o['specifications'])]
        names = {o['product'] for o in choices if o['product']}
        title = next(iter(names)) if len(names)==1 else r[3] or item.get('product') or 'Package'
        if lines: lines.append('')
        lines.append(('Test receipt: ' if preview else 'Received: ') + plain(title))
        lines.append('Catalog: ' + plain(r[5]) + (' | ' + plain(r[6]) if r[6] else ''))
        if match['confirmed_order']:
            lines.append('Matched order: ' + plain(match['confirmed_order']) + ' (order/tracking reference).')
        elif match['candidates']:
            lines.append('Possible order' + ('s' if len(match['candidates'])>1 else '') + ': ' +
                         ', '.join(plain(x) for x in match['candidates']) + ' — catalog/supplier match; order not confirmed.')
        else:
            lines.append('No matching order found in the sheet yet.')
        if r[8] != '' and r[9]:
            lines.append(f'{"Would record" if preview else "This receipt"}: {plain(r[8])} {plain(r[9])}.')
            q = infer_quantity(item,orders.values(),record.get('caption',''))
            if q and q['quantity']==r[8] and q['unit']==r[9]:
                lines.append(q['note'])
        else:
            lines.append('Received quantity is not established from this photo/message.')
        matches = [(row,v) for row,v in inventory.items()
                   if same_product(r[4],r[5],r[6],v[2],v[3])]
        spec_conflict = any(same_product(r[4],r[5],'',o['supplier'],o['catalog']) and
                            not same_product(r[4],r[5],r[6],o['supplier'],o['catalog'],o['specifications'])
                            for o in orders.values())
        if spec_conflict:
            matches = []
            lines.append('Specifications conflict with a catalog-matched order; inventory totals are withheld for review.')
        inv_row = None
        if len(matches)==1:
            inv_row,v=matches[0]
            value = lambda x: 'unknown' if x=='' else plain(x)
            lines.append(f'{"Current real inventory (excludes this test)" if preview else "Inventory delivery totals"}: {value(v[4])} ordered / {value(v[5])} received / '
                         f'{value(v[6])} outstanding ({plain(v[7])}).')
            lines.append('Reconciliation: ' + plain(v[8]) + '. Usage is not deducted.')
            links.append({'text':'Open Inventory', 'url':row_link(config,'Inventory',inv_row)})
        if not preview:
            links.append({'text':'Open receipt','url':row_link(config,'Package receipts',receipt_row)})
        if len(choices)==1:
            links.append({'text':'Open order','url':row_link(config,'Orders',choices[0]['row'])})
        if r[11]: lines.append('Reported stored at: ' + plain(r[11]))
        elif r[10]: lines.append('Intended location: ' + plain(r[10]) + ' (placement not confirmed).')
        contexts.append({'receipt_id':rid,'match':match,'inventory_row':inv_row})
    if not lines: return None
    unique = {v['url']:v for v in links}
    return {'text':'\n'.join(lines)[:11000], 'links':list(unique.values())[:5], 'matches':contexts}


class ReceiptReply:
    def __init__(self, root, channel_id, config):
        self.root, self.channel_id, self.config = Path(root),channel_id,config
        self.receipts = GoogleReceiptStore(config)
        self.orders = GoogleOrderStore(config)
        self.inventory = GoogleInventoryStore(config)

    def prepare(self,result,record,manifest):
        lock = InstanceLock(self.root.parent/'sheet-sync.lock')
        lock.acquire(timeout=45)
        try:
            orders=live_orders(self.orders.snapshot())
            state=ReceiptSync(self.root,self.channel_id,self.receipts).process(manifest)
            if state['status']!='synced':
                raise SheetSyncError('reply_receipt_sync_pending')
            sync_quantities(self.root,self.receipts,orders.values())
            sync_matches(orders,self.receipts,self.root.parent/'reply-receipt-matches.json')
            if self.config.get('inventory_sync_enabled'):
                report=sync_inventory(self.root,self.receipts)
                if report['status']!='ok':
                    raise SheetSyncError('reply_inventory_needs_review')
                inventory=self.inventory.snapshot()
            else:
                inventory={}
            return build_reply(result,record,orders,inventory,self.receipts.snapshot(),self.config)
        finally:
            lock.release()
