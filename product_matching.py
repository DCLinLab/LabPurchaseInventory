"""Match product identity conservatively, without using similar names alone."""

import re
from decimal import Decimal

from receipt_quantity import normalized


def supplier_key(value):
    key = normalized(value)
    return 'FISHERSCIENTIFIC' if 'FISHERSCIENTIFIC' in key else key


def volumes(text):
    result = set()
    for number, unit in re.findall(r'(?i)\b(\d+(?:\.\d+)?)\s*(ul|µl|μl|ml|l)\b', text or ''):
        factor = {'ul': 1, 'µl': 1, 'μl': 1, 'ml': 1000, 'l': 1000000}[unit.lower()]
        result.add(Decimal(number) * factor)
    return result


def same_product(brand, catalog, specs, other_brand, other_catalog, other_specs=''):
    if not normalized(catalog) or normalized(catalog) != normalized(other_catalog):
        return False
    a, b = supplier_key(brand), supplier_key(other_brand)
    if not a or not b or not (a == b or a in b or b in a):
        return False
    left, right = volumes(specs), volumes(other_specs)
    return not (left and right and left.isdisjoint(right))


def receipt_order_match(receipt, orders):
    r = list(receipt) + [''] * max(0, 18-len(receipt))
    candidates, exact = [], []
    for order in orders.values():
        if not same_product(r[4],r[5],r[6],order.get('supplier'),order.get('catalog'),order.get('specifications')):
            continue
        order_ids = {normalized(v) for v in (order.get('order_id'),order.get('purchase_order')) if v}
        tracking = {normalized(v) for v in order.get('tracking',[]) if v}
        if r[15] and normalized(r[15]) not in order_ids:
            continue
        if r[14] and tracking and normalized(r[14]) not in tracking:
            continue
        candidates.append(order['order_id'])
        if (r[15] and normalized(r[15]) in order_ids) or (r[14] and normalized(r[14]) in tracking):
            exact.append(order['order_id'])
    return {'confirmed_order': exact[0] if len(set(exact)) == 1 else None,
            'candidates': sorted(set(candidates))}
