"""Snapshot lab records into an isolated SQLite database for read-only analysis."""

import hashlib
import json
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from inventory_sync import each_quantity
from label_reader import ReaderError
from order_email import parsed_documents
from product_matching import receipt_order_match
from receipt_reply import live_orders, row_link
from sheet_sync import GoogleReceiptStore, SheetSyncError
from order_sync import GoogleOrderStore
from inventory_sync import GoogleInventoryStore


class ReadOnlyRequests:
    def request(self, method, url, **kwargs):
        if method.upper() != 'GET': raise SheetSyncError('query_write_forbidden')
        return super().request(method,url,**kwargs)


class ReadOnlyOrders(ReadOnlyRequests,GoogleOrderStore): pass
class ReadOnlyInventory(ReadOnlyRequests,GoogleInventoryStore): pass
class ReadOnlyReceipts(ReadOnlyRequests,GoogleReceiptStore): pass


TABLES = {
    'orders': ['source_ref','order_id','order_date','requester','supplier','product','catalog',
               'specifications','quantity_ordered','unit','progress','tracking','email_link','notes','ordered_each'],
    'inventory': ['source_ref','item_id','product','supplier','catalog','ordered','received','outstanding',
                  'unit','reconciliation','confirmed_storage','source_links','notes'],
    'receipts': ['source_ref','receipt_id','posted_at_utc','posted_by_slack_id','product','supplier','catalog',
                 'specifications','pack_size','quantity_received','unit','intended_storage','confirmed_storage',
                 'lot','expiry_date','tracking','printed_order_id','slack_link','notes','received_each',
                 'confirmed_order_id','candidate_order_ids'],
    'email_events': ['source_ref','message_id','order_id','supplier','requester','order_date','event_date',
                     'kind','status','product','catalog','quantity_ordered','quantity_shipped','unit','pack_size',
                     'shipment','shipment_date','tracking','unit_price','extended_price','currency','status_detail','details'],
}


def date_value(value, timestamp=False):
    try:
        if isinstance(value, (int,float)) and not isinstance(value,bool) and math.isfinite(value):
            date = datetime.fromtimestamp((value-25569)*86400,timezone.utc)
            return date.isoformat() if timestamp else date.date().isoformat()
        if isinstance(value,str):
            date = datetime.fromisoformat(value.replace('Z','+00:00'))
            return date.isoformat() if timestamp else date.date().isoformat()
    except (ValueError,OverflowError,OSError):
        pass
    return None


def cell(value):
    if value == '' or value is None: return None
    if isinstance(value,(dict,list)): return json.dumps(value,ensure_ascii=False)
    if isinstance(value,float) and not math.isfinite(value): return None
    return value


def snapshot(orders, inventory, receipts, config, email_root=None):
    data = {name:[] for name in TABLES}
    links = {}
    known_orders = live_orders(orders)
    for table, rows, tab in [('orders',orders,'Orders'),('inventory',inventory,'Inventory'),('receipts',receipts,'Package receipts')]:
        width = {'orders':13,'inventory':12,'receipts':18}[table]
        for row, original in sorted(rows.items()):
            values = list(original)[:width] + [None]*max(0,width-len(original))
            ref = f'{table}:{row}'
            links[ref] = {'text':'Open '+('receipt' if table=='receipts' else table.rstrip('s')),
                          'url':row_link(config,tab,row)}
            if table == 'orders':
                values[1] = date_value(values[1])
                values.append(each_quantity(values[7],values[8],values[6]))
            elif table == 'receipts':
                values[1],values[13] = date_value(values[1],True),date_value(values[13])
                match = receipt_order_match(original,known_orders)
                values += [each_quantity(values[8],values[9],values[7]),match['confirmed_order'],match['candidates']]
            data[table].append(dict(zip(TABLES[table],[ref]+[cell(v) for v in values])))
    if email_root is not None:
        for path in sorted(Path(email_root).glob('*/order.json')):
            parsed = json.loads(path.read_text(encoding='utf-8'))
            if parsed.get('status') != 'parsed': continue
            for d, document in enumerate(parsed_documents(parsed)):
                for i, item in enumerate(document.get('items') or [{}]):
                    ref = f'email:{path.parent.name}:{d}:{i}'
                    values = [ref,document.get('message_id'),document.get('order_id'),document.get('supplier'),
                              document.get('requester'),document.get('order_date'),document.get('event_date'),
                              document.get('kind'),item.get('status') or document.get('status'),
                              item.get('product'),item.get('catalog'),item.get('quantity_ordered'),item.get('quantity_shipped'),
                              item.get('unit'),item.get('pack_size'),item.get('shipment'),item.get('shipment_date'),
                              item.get('tracking'),item.get('unit_price'),item.get('extended_price'),
                              item.get('currency') or document.get('currency'),
                              item.get('status_detail') or document.get('status_detail'),
                              {'document':{k:v for k,v in document.items() if k!='items'},'item':item}]
                    data['email_events'].append(dict(zip(TABLES['email_events'],map(cell,values))))
    return {'tables':data,'links':links}


class QueryDatabase:
    """No filesystem database, extensions, writes, PRAGMAs, or unbounded SQL work."""
    def __init__(self, data):
        self.connection = sqlite3.connect(':memory:')
        self.connection.enable_load_extension(False)
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH,16000)
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,250000)
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH,80)
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT,20)
        for name, columns in TABLES.items():
            self.connection.execute('CREATE TABLE '+name+' ('+','.join('"'+c+'"' for c in columns)+')')
            self.connection.executemany('INSERT INTO '+name+' VALUES ('+','.join('?' for _ in columns)+')',
                                        [tuple(row[c] for c in columns) for row in data['tables'][name]])
        self.connection.commit()
        self.connection.execute('PRAGMA query_only=ON')
        self.connection.set_authorizer(self.authorize)
        self.data = data

    @staticmethod
    def authorize(action, first, second, database, trigger):
        if action == sqlite3.SQLITE_READ:
            return sqlite3.SQLITE_OK if first in TABLES else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_DENY if str(second).lower() in ('load_extension','writefile','readfile') else sqlite3.SQLITE_OK
        return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT,sqlite3.SQLITE_RECURSIVE) else sqlite3.SQLITE_DENY

    def describe(self):
        return {name:{'columns':columns,'row_count':len(self.data['tables'][name]),
                      'samples_incomplete_long_text_shortened':True,
                      'sample_rows':[{k:(v[:500] if isinstance(v,str) else v) for k,v in row.items()}
                                     for row in self.data['tables'][name][:8]]} for name,columns in TABLES.items()}

    def execute(self, sql):
        deadline = time.monotonic()+2
        steps = [0]
        def stop():
            steps[0] += 1
            return steps[0]>2000 or time.monotonic()>deadline
        self.connection.set_progress_handler(stop,1000)
        try:
            cursor = self.connection.execute(sql)
            rows = cursor.fetchmany(201)
            columns = [d[0] for d in cursor.description]
            if len(set(columns)) != len(columns): raise ReaderError('duplicate_result_columns_use_aliases')
            result = {'columns':columns,'rows':[list(r) for r in rows[:200]],'truncated':len(rows)>200}
            try: encoded = json.dumps(result,ensure_ascii=False)
            except (TypeError,ValueError) as error:
                raise ReaderError('query_result_requires_text_or_numbers') from error
            if len(encoded)>80000:
                raise ReaderError('query_result_too_large_use_aggregation_or_narrow_columns')
            return result
        except sqlite3.Error as error:
            raise ReaderError('read_only_query_error: '+str(error)[:180]) from error
        finally:
            self.connection.set_progress_handler(None,0)

    def close(self):
        self.connection.close()


def fingerprint(data):
    return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
