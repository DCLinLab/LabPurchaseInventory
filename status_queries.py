"""Semantic Slack requests with deterministic, read-only sheet execution."""

import json
import logging
import re
import time
from pathlib import Path
from threading import Event, Thread

from inventory_sync import GoogleInventoryStore, each_quantity
from label_reader import plain, slack_payload, ReaderError
from query_semantics import SemanticQueryReader, catalog, validate_plan, plan_period
from order_sync import GoogleOrderStore
from photo_intake import write_json
from product_matching import same_product, receipt_order_match
from receipt_reply import live_orders, row_link
from receipt_quantity import normalized
from runtime_lock import InstanceLock
from sheet_sync import GoogleReceiptStore

LOG = logging.getLogger('labpurchase.queries')
def dated_receipts(item, receipts, period):
    selected=[];seen=set();undated=0
    for row,original in receipts.items():
        r=list(original)+['']*max(0,18-len(original))
        if not same_product(item[2],item[3],'',r[4],r[5],r[6]):continue
        key=(r[16] or r[0],normalized(r[5]))
        if key in seen:continue
        seen.add(key)
        if not isinstance(r[1],(int,float)) or isinstance(r[1],bool):
            undated+=1;continue
        stamp=(r[1]-25569)*86400  # Sheets dates in this tab store Slack UTC posting time.
        if period[0]<=stamp<period[1]:selected.append((row,r))
    return selected,undated


def clean_query(text):
    text=re.sub(r'<@[A-Z0-9]+>', '', text or '').strip()[:1500]
    return re.sub(r"(?i)\b(what|where|when)['’]s\b",r'\1 is',text)


def answer_query(text, orders, inventory, receipts, config, now=None, plan=None):
    if plan is None: raise ReaderError('missing_query_plan')
    validate_plan(plan, catalog(orders, inventory, receipts), time.time() if now is None else now)
    if plan['action'] == 'ignore': return None
    if plan['action'] == 'clarify': return {'text': plan['clarification'], 'links': []}
    kind = plan['action']; period = plan_period(plan)
    generic = plan['selection'] == 'all'
    records = [{'row':row, 'values':values}
               for row, values in (orders if kind == 'orders' else inventory).items()]
    selected = records if plan['selection'] == 'all' else [r for r in records
        if f"{kind}:{r['row']}" in plan['record_keys']]
    if not selected:
        if generic:
            return {'text':'There are no tracked '+kind+' entries in the sheet yet.','links':[]}
        return {'text':"I couldn't find a matching " + ('order' if kind=='orders' else 'inventory item') +
                ' in the tracked sheet. Try an order ID, catalog number, or the product name from the label. '
                'This does not establish that the lab has none.', 'links':[]}
    if period:
        period_matches=[];undated=0
        for match in selected:
            found,missing=dated_receipts(match['values'],receipts,period)
            undated+=missing
            if found:period_matches.append((match,found))
        lines=['Inventory added in '+period[2]+':'];links=[]
        if not period_matches:lines.append('No matching package receipts were recorded in this period.')
        for match,found in period_matches[:5]:
            r=match['values'];known=[]
            for _,receipt in found:known.append(each_quantity(receipt[8],receipt[9],receipt[7]))
            lines += ['',plain(r[1])+' | Catalog: '+plain(r[3]),
                      'Received in this period: '+str(sum(q for q in known if q is not None))+' each'+
                      (' (known subtotal; '+str(known.count(None))+' receipt(s) have unknown contents/quantity).' if None in known else '.'),
                      'Package receipt records: '+str(len(found))+'.']
            links.append({'text':'Open Inventory','url':row_link(config,'Inventory',match['row'])})
            links.append({'text':'Open receipt','url':row_link(config,'Package receipts',found[0][0])})
        if len(period_matches)>5:lines.append(str(len(period_matches)-5)+' more items; include a product or catalog number to narrow the results.')
        if undated:lines.append(str(undated)+' matching receipt(s) with missing/unusable dates were excluded.')
        lines.append('Dates use Slack photo posting time (UTC), not a verified physical arrival time. These are receipts, not stock remaining after usage.')
        return {'text':'\n'.join(lines)[:11000],'links':links[:5]}
    lines=['From the current sheet:'];links=[]
    all_orders=live_orders(orders)
    v=lambda x: 'unknown' if x=='' or x is None else plain(x)
    for match in selected[:5]:
        r=match['values'];row=match['row']
        if kind=='orders':
            lines += ['',plain(r[4])+' — order '+plain(r[0]),'Catalog: '+plain(r[5])+(' | '+plain(r[6]) if r[6] else ''),
                      'Ordered: '+v(r[7])+' '+v(r[8])+'. Status: '+v(r[9])+'.']
            if r[10]: lines.append('Tracking: '+plain(r[10]))
            detail = re.search(r'Email status detail: (.*?)(?:; Invoice:|; Shipment quantity|$)',str(r[12]))
            if detail:
                lines.append('Supplier update: '+plain(detail[1]))
            elif plan['wants_eta']:
                lines.append('No delivery ETA is recorded in the current order fields.')
            confirmed=[]
            for receipt in receipts.values():
                if receipt_order_match(receipt,all_orders)['confirmed_order']==r[0] and same_product(receipt[4],receipt[5],receipt[6],r[3],r[5],r[6]):
                    confirmed.append(receipt)
            known=[p for p in confirmed if len(p)>9 and p[8]!='' and p[9]]
            if known:
                totals={}
                for p in known:
                    if isinstance(p[8],(int,float)):totals[p[9]]=totals.get(p[9],0)+p[8]
                lines.append('Receipts linked to this order: '+', '.join(f'{n} {plain(u)}' for u,n in totals.items())+'.')
                if len(known)!=len(confirmed):lines.append('Some linked receipts have unknown quantities.')
            else:lines.append('No quantified lab receipt is confirmed for this order; shipment status alone does not prove receipt.')
            links.append({'text':'Open order '+plain(r[0]),'url':row_link(config,'Orders',row)})
            for inv_row,item in inventory.items():
                if same_product(r[3],r[5],r[6],item[2],item[3]):
                    lines.append('Product inventory: '+v(item[5])+' '+v(item[7])+' received; '+v(item[8])+'. These totals are not order-specific.')
                    links.append({'text':'Open Inventory','url':row_link(config,'Inventory',inv_row)})
        else:
            lines += ['',plain(r[1])+' | Catalog: '+plain(r[3]),
                      'Delivery totals: '+v(r[4])+' ordered / '+v(r[5])+' received / '+v(r[6])+' outstanding ('+v(r[7])+').',
                      'Reconciliation: '+v(r[8])+'.',
                      'Confirmed storage: '+(plain(r[9]) if r[9] else 'not recorded')+'.']
            lines.append('These are cumulative deliveries, not current stock on hand; usage and opening stock are not tracked.')
            links.append({'text':'Open Inventory','url':row_link(config,'Inventory',row)})
    if len(selected)>5:lines.append(f'\n{len(selected)-5} more matches; include a catalog number or product specification to narrow the results.')
    return {'text':'\n'.join(lines)[:11000],'links':links}


class StatusQueryWorker:
    def __init__(self,root,channel_id,client,config,clock=time.time,interpreter=None):
        self.root,self.channel_id,self.client,self.config,self.clock=Path(root),channel_id,client,config,clock
        self.root.mkdir(parents=True,exist_ok=True)
        self.orders=GoogleOrderStore(config);self.inventory=GoogleInventoryStore(config);self.receipts=GoogleReceiptStore(config)
        self.stop=Event()
        self.interpreter = interpreter or SemanticQueryReader()

    def capture(self,event):
        if event.get('channel') != self.channel_id or event.get('files'): return False
        text = clean_query(event.get('text', ''))
        if not text or text.casefold() in {'ping', 'test'}: return False
        stamp=event.get('ts','');thread=event.get('thread_ts') or stamp
        if not re.fullmatch(r'\d+\.\d+',stamp) or not re.fullmatch(r'\d+\.\d+',thread):return False
        # The standalone receiver serializes duplicate deliveries; the worker is
        # the sole writer after this exclusive creation.
        path=self.root/(self.channel_id+'_'+stamp+'.json')
        try:
            with path.open('x',encoding='utf-8') as f:
                json.dump({'status':'pending','channel':self.channel_id,'thread_ts':thread,
                           'text':clean_query(event.get('text','')),'message_ts':stamp},f)
        except FileExistsError:pass
        return True

    def process(self,path):
        state=json.loads(path.read_text(encoding='utf-8'))
        if state['channel']!=self.channel_id or not re.fullmatch(r'\d+\.\d+',state['thread_ts']):raise ValueError('wrong_query_destination')
        if state['status']=='posting':
            state['status']='delivery_uncertain';write_json(path,state)
        if state['status'] in ('sent','ignored','delivery_uncertain','needs_review') or self.clock()<state.get('retry_at',0):return state
        cooldown_path = self.root / 'reader-cooldown.json'
        cooldown = json.loads(cooldown_path.read_text()) if cooldown_path.exists() else {}
        if self.clock() < cooldown.get('retry_at', 0): return state
        try:
            lock=InstanceLock(self.root.parent/'sheet-sync.lock');lock.acquire(timeout=45)
            try:
                orders, inventory, receipts = self.orders.snapshot(), self.inventory.snapshot(), self.receipts.snapshot()
            finally:lock.release()
            records = catalog(orders, inventory, receipts)
            # Cache interpretation against the candidate catalog. Sheet quantities
            # may change between retries; rendering always uses the new snapshot.
            import hashlib
            fingerprint = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
            if not state.get('interpretation') or state.get('catalog_fingerprint') != fingerprint:
                context = []
                for other in sorted(self.root.glob(self.channel_id + '_*.json')):
                    prior = json.loads(other.read_text(encoding='utf-8'))
                    if prior.get('thread_ts') == state['thread_ts'] and float(prior['message_ts']) < float(state['message_ts']):
                        context.append({'message': prior['text'], 'reply': prior.get('reply', {}).get('text', '')[:2000]})
                state['interpretation_attempts'] = state.get('interpretation_attempts', 0) + 1
                write_json(path, state)
                state['interpretation'] = self.interpreter.interpret(state['text'], records, self.clock(), context[-6:])
                state['catalog_fingerprint'] = fingerprint
                state['interpreted_at'] = self.clock()
                write_json(path, state)
            reply=answer_query(state['text'],orders,inventory,receipts,self.config,
                               now=state['interpreted_at'],plan=state['interpretation']['fields'])
        except Exception as error:
            code = str(error) if isinstance(error, ReaderError) else type(error).__name__
            quota = code == 'codex_usage_limit'
            state.update(status='waiting_usage' if quota else 'retry_wait',error=code,
                         retry_at=self.clock()+(1800 if quota else 60))
            if quota:
                state['interpretation_attempts'] = max(0, state.get('interpretation_attempts', 1) - 1)
                write_json(cooldown_path, {'retry_at':state['retry_at']})
            elif isinstance(error, ReaderError) and state.get('interpretation_attempts', 0) >= 3:
                state['status'] = 'needs_review'
            LOG.warning('Query queued ts=%s error=%s status=%s', state['message_ts'], code, state['status'])
            write_json(path,state);return state
        if not reply:
            state['status']='ignored';write_json(path,state);return state
        state.update(status='posting',reply=reply);write_json(path,state)
        try:
            response=self.client.chat_postMessage(channel=self.channel_id,thread_ts=state['thread_ts'],**slack_payload(reply['text'],reply['links']))
            state.update(status='sent',reply_ts=response['ts'],sent_at=self.clock())
        except Exception as error:
            response=getattr(error,'response',None)
            if response is not None and response.get('ok') is False:
                state.update(status='retry_wait',retry_at=self.clock()+60,error=response.get('error','slack_rejected'))
            else:state.update(status='delivery_uncertain',error=type(error).__name__)
        write_json(path,state);return state

    def run(self):
        while not self.stop.is_set():
            for path in sorted(self.root.glob(self.channel_id+'_*.json')):
                if self.stop.is_set():return
                try:self.process(path)
                except Exception as error:LOG.warning('Status query failed: %s',type(error).__name__)
            self.stop.wait(3)

    def start(self):
        self.thread=Thread(target=self.run,name='status-queries',daemon=True);self.thread.start()
