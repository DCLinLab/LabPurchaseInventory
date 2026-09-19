"""Durable Slack notifications for meaningful email-derived order changes."""

import hashlib
import json
import time

from label_reader import plain, slack_payload
from order_sync import order_key, order_row
from photo_intake import write_json
from sheet_sync import SheetSyncError


def status_fingerprint(order):
    # New forwarding/message IDs alone do not mean a new order status.
    state = {k: order.get(k) for k in ('order_id','supplier','catalog','quantity_ordered',
             'unit','pack_size','quantity_shipped','shipments','tracking','invoices')}
    if order.get('semantic_version'):
        state.update({k:order.get(k) for k in ('status','status_detail','invoice_numbers','shipment_total_unknown')})
    return hashlib.sha256(json.dumps(state,sort_keys=True).encode()).hexdigest()


def status_text(order):
    shipped, total = order['quantity_shipped'],order['quantity_ordered']
    unit = order.get('unit') or 'unit unknown'
    lines = ['Order email update: ' + plain(order['product']),
             'Supplier: ' + plain(order['supplier']) + ' | Order: ' + plain(order['order_id']),
             'Catalog: ' + plain(order['catalog'])]
    if order.get('specifications') or order.get('pack_size'):
        lines.append('Details: ' + plain('; '.join(v for v in (order.get('specifications'),order.get('pack_size')) if v)))
    if order.get('semantic_version'):
        from order_reconcile import progress
        lines.append('Status: '+progress(order)+'.')
        if total is not None: lines.append(f'Ordered: {total:g} {plain(unit)}(s).')
        if order.get('status_detail'): lines.append(plain(order['status_detail']))
        if order.get('invoice_numbers'): lines.append('Invoice received: '+', '.join(plain(i) for i in order['invoice_numbers'])+'; payment status not inferred.')
    else:
        lines.append(f'Shipped: {shipped}' + (f' of {total}' if total is not None else '; ordered total unknown') + f' {plain(unit)}(s).')
    for shipment in order['shipments']:
        lines.append(f"Shipment {plain(shipment['number'])}: {shipment['quantity']} {plain(unit)}(s)" +
                     (f"; shipped {plain(shipment['date'])}" if shipment.get('date') else '') + '.')
    if order.get('tracking'):
        lines.append('Tracking: ' + ', '.join(plain(t) for t in order['tracking']))
    if order.get('invoices'):
        lines.append('Invoice notification received: ' + ', '.join(plain(i) for i in order['invoices']) + ' (notification only; not a bill to pay).')
    lines.append('Shipment status comes from email; lab receipt is tracked separately from package photos.')
    return '\n'.join(lines)[:11000]


def review_messages(root, conflicts):
    reviews=[]
    for path in sorted(root.glob('*/order.json')):
        result=json.loads(path.read_text(encoding='utf-8'))
        from order_email import parsed_documents
        conflict=any(order_key(document['supplier'],document['order_id'],i['catalog']) in conflicts
                     for document in parsed_documents(result) for i in document.get('items',[]))
        if result.get('status')!='needs_review' and not conflict:
            continue
        source=json.loads(path.with_name('source.json').read_text(encoding='utf-8'))
        reviews.append({'message_id':source['id'],'subject':source.get('subject','')})
    return reviews


class OrderNotifier:
    def __init__(self, path, client, channel_id, sheet_config, clock=time.time):
        self.path,self.client,self.channel_id,self.sheet_config,self.clock = path,client,channel_id,sheet_config,clock

    def load(self):
        journal = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {
            'channel_id':self.channel_id, 'spreadsheet_id':self.sheet_config['spreadsheet_id'], 'orders':{}, 'events':{}}
        if journal['channel_id']!=self.channel_id or journal['spreadsheet_id']!=self.sheet_config['spreadsheet_id']:
            raise SheetSyncError('order_notification_destination_changed')
        return journal

    def baseline(self, orders, review_ids=()):
        if self.path.exists():
            self.load()
            return
        journal=self.load()
        journal['orders']={key:status_fingerprint(o) for key,o in orders.items()}
        journal['baseline_review_ids']=list(review_ids)
        journal['enabled_at']=self.clock()
        write_json(self.path,journal)

    def deliver(self,journal,event_id,text,links):
        state=journal['events'].get(event_id,{})
        if state.get('status')=='posting':
            state['status']='delivery_uncertain'
            write_json(self.path,journal)
        if state.get('status') in ('sent','delivery_uncertain') or self.clock()<state.get('retry_at',0):
            return state.get('status','retry_wait')
        state={'status':'posting','text':text,'started_at':self.clock()}
        journal['events'][event_id]=state
        write_json(self.path,journal)
        try:
            response=self.client.chat_postMessage(channel=self.channel_id,**slack_payload(text,links))
            if not response.get('ts'):
                raise ValueError('missing_slack_response_timestamp')
            state.update(status='sent',reply_ts=response['ts'],sent_at=self.clock())
        except Exception as error:
            response=getattr(error,'response',None)
            # Only an explicit Slack rejection is safe to retry automatically.
            if response is not None and response.get('ok') is False:
                state.update(status='retry_wait',retry_at=self.clock()+300,error=response.get('error','slack_rejected'))
            else:
                state.update(status='delivery_uncertain',error=type(error).__name__)
        write_json(self.path,journal)
        return state['status']

    def run(self,orders,statuses,store,reviews=()):
        journal=self.load()
        rows=store.snapshot() if any(statuses.get(k)=='synced' and journal['orders'].get(k)!=status_fingerprint(o)
                                     for k,o in orders.items()) else {}
        by_key={order_key(r[3],r[0],r[5]):(row,r) for row,r in rows.items() if r[0] and r[3] and r[5]}
        outcomes=[]
        for key,order in orders.items():
            fingerprint=status_fingerprint(order)
            if statuses.get(key)!='synced' or journal['orders'].get(key)==fingerprint:
                continue
            target=by_key.get(key)
            if not target or target[1]!=order_row(order):
                outcomes.append('needs_review');continue
            event_id=hashlib.sha256((key+'|'+fingerprint).encode()).hexdigest()
            row=target[0]
            link=('https://docs.google.com/spreadsheets/d/'+self.sheet_config['spreadsheet_id']+
                  '/edit#gid='+str(self.sheet_config['tabs']['Orders']['sheet_id'])+'&range=A'+str(row))
            previous=journal['events'].get(event_id,{}).get('status')
            outcome=self.deliver(journal,event_id,status_text(order),[{'text':'Open order','url':link}])
            if outcome=='sent':
                journal['orders'][key]=fingerprint
                write_json(self.path,journal)
                if previous=='sent':continue
            outcomes.append(outcome)
        for review in reviews:
            mid=review['message_id']
            if mid in journal.get('baseline_review_ids',[]):continue
            eid='review-'+hashlib.sha256(mid.encode()).hexdigest()
            if journal['events'].get(eid,{}).get('status') in ('sent','delivery_uncertain'):continue
            text=('Order email needs review: '+plain(review.get('subject') or 'Order update')+
                  '\nI could not reliably extract its status. The order status has not been inferred from this email.')
            outcomes.append(self.deliver(journal,eid,text,[]))
        return {'sent':outcomes.count('sent'),'delivery_uncertain':outcomes.count('delivery_uncertain'),
                'retry_wait':outcomes.count('retry_wait'),'needs_review':outcomes.count('needs_review')}
