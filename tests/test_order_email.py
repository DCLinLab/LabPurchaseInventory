import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest

from order_email import OrderEmailError, message_text, parse_fisher
from order_sync import merge_documents, order_key, order_row, sync_orders, match_receipt


BODY = '''From: FisherCustomerService.US@thermofisher.com
Shipping Confirmation
Order Number: A123
P.O. Number: 200100
Order Date: 09/09/2026
Shipment number: 001
Basix Tubes
Catalog number: 0012345
Shipment date: 09/10/2026
Case of 500
3 of 3
Tracking information is currently unavailable
Order Placed By: Attn Test Person
'''
PDF = '''FISHER SCIENTIFIC COMPANY
200100
A123   09/09/2026   888   09/10/2026
 1  3 CS 0012345   15ML CENTRIFUGE TUBE RCK 500CS    68.90   206.70
This is not an Invoice - Do not Remit Payment
 001  09/10/2026
'''


def shipping():
    return {'id': 'abc1', 'thread_id': 'abc1', 'subject': 'Fw: Your order has been shipped PO: 200100 Order: A123',
            'text': BODY, 'pdf_texts': []}


def invoice():
    return {'id': 'abc2', 'thread_id': 'abc2', 'subject': 'Fw: Order# A123 PO# 200100 - INVOICE NOTIFICATION/Fisher Scientific -',
            'text': 'From: FisherCustomerService.US@thermofisher.com', 'pdf_texts': [{'filename': 'invoice.pdf', 'text': PDF}]}


class ParseTests(unittest.TestCase):
    def test_plain_text_preferred_over_html_alternative(self):
        def part(mime, text):
            return {'mimeType': mime, 'body': {'data': base64.urlsafe_b64encode(text.encode()).decode()}}
        payload = {'parts': [part('text/plain', 'Order A123'), part('text/html', '<p>Order A123</p>')]}
        self.assertEqual(message_text(payload), 'Order A123')

    def test_html_fallback_does_not_execute_or_include_scripts(self):
        payload = {'mimeType': 'text/html', 'body': {'data': base64.urlsafe_b64encode(b'<p>Order</p><script>evil()</script>').decode()}}
        self.assertNotIn('evil', message_text(payload))

    def test_shipping_invoice_and_duplicate_forward_merge_without_double_count(self):
        ship, inv = parse_fisher(shipping()), parse_fisher(invoice())
        merged, conflict = merge_documents([ship, inv, copy.deepcopy(ship)])
        self.assertFalse(conflict)
        self.assertEqual(len(merged), 1)
        order = next(iter(merged.values()))
        self.assertEqual((order['quantity_ordered'], order['quantity_shipped']), (3, 3))
        row = order_row(order)
        self.assertEqual(row[5], '0012345')
        self.assertEqual(row[7:10], [3, 'case', 'Shipped 3 of 3'])
        self.assertIn('not a bill to pay', row[12])
        self.assertIn('not confirmed', row[12])
        self.assertEqual(len(order['sources']), 2)

    def test_invoice_quantity_does_not_become_total_quantity_ordered(self):
        merged, _ = merge_documents([parse_fisher(invoice())])
        order = next(iter(merged.values()))
        self.assertIsNone(order['quantity_ordered'])
        self.assertEqual(order_row(order)[7], '')

    def test_conflicting_shipped_quantities_block_the_line(self):
        inv = parse_fisher(invoice())
        inv['items'][0]['quantity_shipped'] = 4
        merged, conflicts = merge_documents([parse_fisher(shipping()), inv])
        self.assertFalse(merged)
        self.assertEqual(next(iter(conflicts.values())), 'conflicting_shipment_quantity')

    def test_changed_supplier_missing_pdf_and_incomplete_items_need_review(self):
        for source in (dict(shipping(), text=BODY.replace('thermofisher.com', 'other.invalid')),
                       dict(invoice(), pdf_texts=[]), dict(shipping(), text=BODY + '\nCatalog number: 9999999')):
            with self.assertRaises(OrderEmailError):
                parse_fisher(source)

    def test_subject_and_body_order_mismatch_rejected(self):
        with self.assertRaisesRegex(OrderEmailError, 'shipping_order_mismatch'):
            parse_fisher(dict(shipping(), text=BODY.replace('Order Number: A123', 'Order Number: B321')))

    def test_catalog_match_is_not_proof_of_order_or_received_quantity(self):
        merged, _ = merge_documents([parse_fisher(shipping())])
        receipt = [''] * 18
        receipt[4], receipt[5] = 'basix / Fisher Scientific', '001-2345'
        self.assertEqual(match_receipt(receipt, merged), {'confirmed_order': None, 'candidates': ['A123']})
        receipt[15] = '200100'
        self.assertEqual(match_receipt(receipt, merged)['confirmed_order'], 'A123')
        receipt[15] = 'ANOTHER'
        self.assertIsNone(match_receipt(receipt, merged)['confirmed_order'])


class FakeStore:
    def __init__(self):
        self.config = {'spreadsheet_id': 'test'}
        self.rows = {}
        self.writes = 0
        self.fail = False

    def snapshot(self):
        return copy.deepcopy(self.rows)

    def read_row(self, row):
        return self.rows.get(row, [''] * 13)

    def write_row(self, row, values, expected):
        if self.read_row(row) != expected:
            raise ValueError('conflict')
        self.rows[row] = values
        self.writes += 1
        if self.fail:
            raise TimeoutError()


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'sync.json'
        self.store = FakeStore()
        self.orders, _ = merge_documents([parse_fisher(shipping()), parse_fisher(invoice())])

    def test_retry_after_timeout_and_sort_is_idempotent(self):
        self.store.fail = True
        with self.assertRaises(TimeoutError):
            sync_orders(self.orders, self.store, self.path)
        self.store.rows[9] = self.store.rows.pop(5)
        self.store.fail = False
        statuses = sync_orders(self.orders, self.store, self.path)
        self.assertEqual(list(statuses.values()), ['synced'])
        self.assertEqual(self.store.writes, 1)

    def test_manual_edits_not_overwritten_by_later_mail(self):
        sync_orders(self.orders, self.store, self.path)
        self.store.rows[5][2] = 'Changed by user'
        next(iter(self.orders.values()))['requester'] = 'New name'
        statuses = sync_orders(self.orders, self.store, self.path)
        self.assertEqual(list(statuses.values()), ['needs_review'])
        self.assertEqual(self.store.rows[5][2], 'Changed by user')
        self.assertEqual(self.store.writes, 1)

    def test_later_invoice_updates_same_order_row(self):
        initial, _ = merge_documents([parse_fisher(shipping())])
        sync_orders(initial, self.store, self.path)
        sync_orders(self.orders, self.store, self.path)
        self.assertEqual(len(self.store.rows), 1)
        self.assertIn('Invoice notification 888', self.store.rows[5][12])


if __name__ == '__main__':
    unittest.main()
