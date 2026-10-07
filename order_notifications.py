"""Durable Slack notifications for meaningful email-derived order changes."""

import html
import re
from datetime import datetime
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


def display_value(value):
    # Decode HTML, then neutralize email-supplied Slack mentions and formatting.
    value=plain(html.unescape(str(value)))
    for old,new in (('*','∗'),('_','＿'),('`','ʼ'),('~','～')):
        value=value.replace(old,new)
    return value.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')


def status_text(order):
    from order_reconcile import STATUS_LABELS
    value=display_value
    product=order['product'];brand=None
    if product.lower().startswith('medchemexpress,'):
        brand='MedChemExpress';product=product.split(',',1)[1].strip()
    lines=['📦 *Order update — '+value(product)+'*',
           '*Status: '+value(STATUS_LABELS.get(order.get('status'),'Status not recorded'))+'*']
    detail=html.unescape(order.get('status_detail') or '').strip()
    match=re.fullmatch(r'Estimated delivery(?: date)?:\s*(\d{2}/\d{2}/\d{4})',detail,re.I)
    if match:
        try:detail='Estimated delivery: '+datetime.strptime(match[1],'%m/%d/%Y').strftime('%B %d, %Y').replace(' 0',' ')
        except ValueError:pass
    if detail:lines.append(value(detail))
    lines.append('')
    def field(label,text):
        if text is not None and text!='':lines.append('• *'+label+':* '+value(text))
    field('Brand',brand);field('Supplier',order['supplier'])
    field('Order #',order['order_id']);field('Catalog #',order['catalog'])
    specs=[v.strip() for v in html.unescape(order.get('specifications') or '').split(';') if v.strip()]
    amount=next((v for v in specs if re.fullmatch(r'\d+(?:\.\d+)?\s*(?:mg|g|kg|µg|ug|mL|ml|L)',v)),None)
    total=order.get('quantity_ordered');unit=order.get('unit') or 'units (type unknown)'
    combined=amount and unit.lower()=='each' and (order.get('pack_size') or '').lower() in ('','each of 1') and total is not None
    if combined:
        amount_display=re.sub(r'(?<=\d)(?=[A-Za-zµ])',' ',amount)
        field('Quantity',f'{total:g} × {amount_display}');specs.remove(amount)
    elif total is not None:field('Quantity',f'{total:g} {unit}')
    else:field('Quantity','Not confirmed')
    if not combined:field('Pack size',order.get('pack_size'))
    for spec in specs:
        m=re.fullmatch(r'(CAS|Purity):\s*(.+)',spec,re.I)
        if m:field('CAS' if m[1].lower()=='cas' else 'Purity',m[2])
        else:field('Details',spec)
    for shipment in order.get('shipments',[]):
        field('Shipment '+str(shipment['number']),str(shipment['quantity'])+' '+unit+(' — '+shipment['date'] if shipment.get('date') else ''))
    if order.get('tracking'):field('Tracking',', '.join(order['tracking']))
    invoices=order.get('invoice_numbers') or order.get('invoices')
    if invoices:
        note=' (payment status not inferred)' if order.get('invoice_numbers') else ' (notification only; not a bill to pay)'
        field('Invoice',', '.join(invoices)+note)
    shipped=order.get('quantity_shipped')
    lines.extend(['',('Shipped quantity is not confirmed.' if shipped is None else f'Shipped: {shipped:g}'+(f' of {total:g}' if total is not None else '')+' '+value(unit)+'.'),
                  'Lab receipt is tracked separately.'])
    return '\n'.join(lines)[:11000]


def order_payload(text,links):
    payload=slack_payload(text,links)
    payload['text']=text;payload['mrkdwn']=True
    for block in payload['blocks']:
        if block['type']=='section':
            block['text']={'type':'mrkdwn','text':block['text']['text'],'verbatim':True}
    return payload


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
            response=self.client.chat_postMessage(channel=self.channel_id,**order_payload(text,links))
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
        announced=journal.setdefault('announced_orders', {})
        for key in journal['orders']:
            announced.setdefault(key.rsplit('|', 1)[0], 'previously_recorded')
        groups={}
        for key,order in orders.items():
            group=order_key(order['supplier'],order['order_id'],'').rsplit('|',1)[0]
            groups.setdefault(group,[]).append((key,order))
        pending={g:items for g,items in groups.items() if g not in announced}
        rows=store.snapshot() if any(all(statuses.get(k)=='synced' for k,o in items)
                                    for items in pending.values()) else {}
        by_key={order_key(r[3],r[0],r[5]):(row,r) for row,r in rows.items() if r[0] and r[3] and r[5]}
        outcomes=[]
        for group,items in pending.items():
            if not all(statuses.get(k)=='synced' for k,o in items):continue
            # Preserve uncertain deliveries from the old per-status journal.
            legacy=[hashlib.sha256((k+'|'+status_fingerprint(o)).encode()).hexdigest() for k,o in items]
            uncertain=False
            for eid in legacy:
                state=journal['events'].get(eid,{})
                if state.get('status') in ('posting','delivery_uncertain','sent'):
                    if state['status']=='posting':state['status']='delivery_uncertain'
                    announced[group]=state['status'];outcomes.append(state['status'])
                    uncertain=True;break
            if uncertain:continue
            if any(k not in by_key or by_key[k][1]!=order_row(o) for k,o in items):
                outcomes.append('needs_review');continue
            event_id='first-order-'+hashlib.sha256(group.encode()).hexdigest()
            row=by_key[items[0][0]][0]
            link=('https://docs.google.com/spreadsheets/d/'+self.sheet_config['spreadsheet_id']+
                  '/edit#gid='+str(self.sheet_config['tabs']['Orders']['sheet_id'])+'&range=A'+str(row))
            text='\n\n'.join(status_text(o) for k,o in items)[:11000]
            outcome=self.deliver(journal,event_id,text,[{'text':'Open order','url':link}])
            if outcome in ('sent','delivery_uncertain'):announced[group]=outcome
            outcomes.append(outcome)
        # Later emails continue syncing, without another channel announcement.
        for key,order in orders.items():
            if statuses.get(key)=='synced' and key.rsplit('|',1)[0] in announced:
                journal['orders'][key]=status_fingerprint(order)
        write_json(self.path,journal)
        return {'sent':outcomes.count('sent'),'delivery_uncertain':outcomes.count('delivery_uncertain'),
                'retry_wait':outcomes.count('retry_wait'),'needs_review':outcomes.count('needs_review')+len(reviews)}
