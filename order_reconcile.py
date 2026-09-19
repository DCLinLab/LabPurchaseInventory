"""Merge semantic order facts without treating repeated emails as new shipments."""

from datetime import datetime, timezone
import copy
from order_email import OrderEmailError, normalize


STATUS_LABELS={'confirmed':'Confirmed','processing':'Processing','shipped':'Shipped',
    'partially_shipped':'Partially shipped','backordered':'Backordered','delayed':'Delayed',
    'cancelled':'Cancelled','carrier_delivered':'Carrier reports delivered','unknown':'Status not recorded'}


def unique(values,name):
    values=[v for v in values if v is not None and v!='']
    if name in ('supplier','order_id','purchase_order','requester'):
        seen=set();distinct=[]
        for value in values:
            normalized=normalize(value)
            if normalized not in seen:distinct.append(value);seen.add(normalized)
        values=distinct
    else:values=list(dict.fromkeys(values))
    if len(values)>1:raise OrderEmailError('conflicting_'+name)
    return next(iter(values),None)


def event_time(doc,item):
    stamp=doc.get('event_date') or item.get('shipment_date')
    if stamp:return datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).timestamp()
    return float(doc.get('observed_ms',0))/1000


def merge_line(records):
    records=copy.deepcopy(records)
    docs,items=zip(*records)
    from receipt_quantity import contents_per_package
    from product_matching import volumes
    target_unit=next((i['unit'] for i in items if i.get('quantity_ordered') is not None and i.get('unit')),
                     next((i.get('unit') for i in items if i.get('unit')),None))
    factors={unit:1 for unit in ('each','bottle','vial','tube','piece','item')}
    for item in items:
        unit=item.get('unit')
        factor=contents_per_package(item.get('pack_size'),unit)
        if factor:
            if unit in factors and factors[unit]!=factor:raise OrderEmailError('conflicting_pack_size')
            factors[unit]=factor
    for item in items:
        unit=item.get('unit')
        if not unit or unit==target_unit:continue
        for field in ('quantity_ordered','quantity_shipped'):
            if item.get(field) is None:continue
            if unit not in factors or target_unit not in factors:raise OrderEmailError('incompatible_quantity_units')
            value=item[field]*factors[unit]/factors[target_unit]
            item[field]=int(value) if value.is_integer() else value
        # Price per a different sales unit is kept in source extraction, not
        # mislabeled as the target unit's price in the sheet.
        item['unit_price']=None
        item['unit']=target_unit
        item['pack_size']=next((i.get('pack_size') for i in items if i.get('unit')==target_unit and i.get('pack_size')),None)
    order={field:unique([d.get(field) for d in docs],field)
           for field in ('supplier','order_id','purchase_order','order_date','requester')}
    order.update({field:unique([i.get(field) for i in items],field)
                  for field in ('quantity_ordered','unit')})
    sizes=list(dict.fromkeys(i['pack_size'] for i in items if i.get('pack_size')))
    order['pack_size']=(f'{target_unit.title()} of {factors[target_unit]}'
                        if target_unit in factors and target_unit not in ('each','bottle','vial','tube','piece','item')
                        else '; '.join(sizes) or None)
    specs=list(dict.fromkeys(i['specifications'] for i in items if i.get('specifications')))
    measures=[volumes(s) for s in specs if volumes(s)]
    if measures and any(v!=measures[0] for v in measures):raise OrderEmailError('conflicting_specifications')
    order['specifications']='; '.join(specs) or None
    order['catalog']=items[0]['catalog']
    order['product']=next((i['product'] for d,i in records if d['kind']=='shipping_confirmation'),items[0]['product'])
    shipment_groups={};cumulative=[];unallocated=[]
    for doc,item in records:
        quantity=item.get('quantity_shipped')
        if quantity is None:continue
        basis=item.get('shipment_quantity_basis','shipment')
        if basis=='cumulative':
            cumulative.append((event_time(doc,item),quantity));continue
        identifier=item.get('shipment')
        if identifier and str(identifier).isdigit():identifier=str(int(identifier))
        if not identifier and item.get('tracking'):
            identifier='tracking:'+','.join(sorted(item['tracking']))
        if not identifier:
            unallocated.append(quantity);continue
        shipment_groups.setdefault(identifier,[]).append((doc,item))
    order['shipments']=[]
    for identifier,same in sorted(shipment_groups.items()):
        order['shipments'].append({'number':identifier,
            'quantity':unique([i.get('quantity_shipped') for _,i in same],'shipment_quantity'),
            'date':unique([i.get('shipment_date') for _,i in same],'shipment_date')})
    known=sum(s['quantity'] for s in order['shipments'])
    order['quantity_shipped']=known if order['shipments'] and not unallocated else None
    if cumulative:
        latest=max(t for t,_ in cumulative)
        total=unique([q for t,q in cumulative if t==latest],'cumulative_shipped')
        dated_shipments=[event_time(d,i) for group in shipment_groups.values() for d,i in group]
        if total>=known and not any(t>latest for t in dated_shipments):
            order['quantity_shipped']=total
        else:order['quantity_shipped']=None
    if order['quantity_ordered'] is not None and order['quantity_shipped'] is not None and order['quantity_shipped']>order['quantity_ordered']:
        raise OrderEmailError('shipped_exceeds_ordered')
    order['tracking']=sorted({t for i in items for t in i.get('tracking',[])})
    order['invoices']=sorted({i['invoice_notification'] for i in items if i.get('invoice_notification')})
    order['invoice_numbers']=sorted({i['invoice_number'] for i in items if i.get('invoice_number')})
    order['sources']=sorted({f"https://mail.google.com/mail/u/?authuser=linjhumse%40gmail.com#all/{d['thread_id']}" for d in docs})
    order['message_ids']=sorted({d['message_id'] for d in docs})
    order['unit_price']=unique([i.get('unit_price') for i in items],'unit_price')
    order['currency']=unique([i.get('currency') for i in items],'currency')
    events=[]
    for doc,item in records:
        status=item.get('status')
        if not status or status=='unknown':status=doc.get('status','unknown')
        if status=='unknown' and not doc.get('semantic_version') and doc['kind']=='shipping_confirmation':status='shipped'
        if status!='unknown':events.append((event_time(doc,item),float(doc.get('observed_ms',0)),status,item.get('status_detail') or doc.get('status_detail')))
    if events:
        events.sort(key=lambda e:(e[0],e[1]));latest=events[-1]
        # No lexical choice between simultaneous contradictory status reports.
        unique([e[2] for e in events if e[:2]==latest[:2]],'order_status')
        order['status'],order['status_detail']=latest[2:]
    else:order['status'],order['status_detail']='unknown',None
    if (order['status']=='shipped' and order['quantity_ordered'] is not None and
            order['quantity_shipped'] is not None and order['quantity_shipped']<order['quantity_ordered']):
        order['status']='partially_shipped'
    order['shipment_total_unknown']=bool(unallocated) and order['quantity_shipped'] is None
    order['semantic_version']=2
    return order


def progress(order):
    label=STATUS_LABELS.get(order.get('status'),'Status not recorded')
    shipped,ordered=order.get('quantity_shipped'),order.get('quantity_ordered')
    if shipped is not None:
        quantity=f'Shipped {shipped:g}'+(f' of {ordered:g}' if ordered is not None else '; ordered total unknown')
        return quantity if label=='Shipped' else label+'; '+quantity
    return label+'; shipped quantity not established'
