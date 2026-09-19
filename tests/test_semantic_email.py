import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from email_reader import validate_extraction, SCHEMA, identifier_in_quote
from label_reader import ReaderError
from order_email import parse_pending, parse_fisher
from order_sync import merge_documents, order_row
from order_notifications import status_text, status_fingerprint
from pack_contents import canonical
from receipt_quantity import infer_quantity
from test_order_email import shipping
from test_receipt_quantity import observed


def semantic(status='confirmed', **changes):
    doc=parse_fisher(shipping())
    doc.update(semantic_version=2,status=status,event_date='2026-09-11',observed_ms='1000')
    item=doc['items'][0]
    item.update(quantity_shipped=None,shipment=None,status='unknown',status_detail=None,
                shipment_quantity_basis='unknown',invoice_number=None,currency=None)
    item.update(changes)
    return doc


class MergeTests(unittest.TestCase):
    def test_equivalent_sales_units_and_spec_wording_reconcile(self):
        ordered=semantic();ordered['items'][0]['specifications']='15 mL'
        ship=semantic('shipped',quantity_ordered=None,quantity_shipped=500,unit='each',
                      shipment='01',shipment_quantity_basis='shipment',pack_size=None,specifications='15mL sterile')
        ship['event_date']='2026-09-12'
        ship['requester']='TEST PERSON'
        merged,conflicts=merge_documents([ordered,ship]);self.assertFalse(conflicts)
        order=next(iter(merged.values()))
        self.assertEqual((order['quantity_ordered'],order['quantity_shipped'],order['unit']),(3,1,'case'))
        self.assertEqual(order['status'],'partially_shipped')
        self.assertEqual(order['pack_size'],'Case of 500')

    def test_confirmation_does_not_claim_zero_shipped_and_missing_metadata_is_allowed(self):
        doc=semantic();doc.update(order_date=None,purchase_order=None,requester=None)
        merged,conflicts=merge_documents([doc]);self.assertFalse(conflicts)
        row=order_row(next(iter(merged.values())))
        self.assertEqual(row[1],'');self.assertEqual(row[7],3)
        self.assertEqual(row[9],'Confirmed; shipped quantity not established')

    def test_duplicate_shipment_and_cumulative_snapshot_do_not_add_twice(self):
        a=semantic('shipped',quantity_shipped=2,shipment='1',shipment_quantity_basis='shipment')
        b=copy.deepcopy(a);b['message_id']='forward2'
        c=semantic('shipped',quantity_shipped=2,shipment_quantity_basis='cumulative')
        c['event_date']='2026-09-12'
        merged,conflicts=merge_documents([a,b,c]);self.assertFalse(conflicts)
        self.assertEqual(next(iter(merged.values()))['quantity_shipped'],2)

    def test_distinct_shipments_add_but_unidentified_shipments_remain_unknown(self):
        a=semantic('partially_shipped',quantity_shipped=1,shipment='001',shipment_quantity_basis='shipment')
        b=copy.deepcopy(a);b['items'][0].update(shipment='002',quantity_shipped=2)
        merged,conflicts=merge_documents([a,b]);self.assertFalse(conflicts)
        self.assertEqual(next(iter(merged.values()))['quantity_shipped'],3)
        a['items'][0]['shipment']=None
        merged,_=merge_documents([a,b]);order=next(iter(merged.values()))
        self.assertIsNone(order['quantity_shipped']);self.assertTrue(order['shipment_total_unknown'])

    def test_invoice_cannot_become_a_shipment_or_notification_only_bill(self):
        doc=semantic('unknown');doc['kind']='invoice'
        doc['items'][0].update(invoice_number='INV-9',unit_price='25.00',currency='EUR')
        merged,_=merge_documents([doc]);order=next(iter(merged.values()))
        self.assertIsNone(order['quantity_shipped'])
        row=order_row(order);self.assertIn('EUR 25.00',row[12])
        self.assertNotIn('USD',row[12]);self.assertNotIn('not a bill',row[12])
        self.assertIn('payment status not inferred',status_text(order))

    def test_latest_supplier_status_changes_notification_without_claiming_lab_receipt(self):
        first=semantic('confirmed');later=semantic('carrier_delivered');later['event_date']='2026-09-12'
        before,_=merge_documents([first]);after,_=merge_documents([first,later])
        a,b=next(iter(before.values())),next(iter(after.values()))
        self.assertNotEqual(status_fingerprint(a),status_fingerprint(b))
        self.assertIn('Carrier reports delivered',status_text(b))
        self.assertIn('lab receipt is tracked separately',status_text(b))


class QueueTests(unittest.TestCase):
    def test_identical_forward_reuses_facts_but_keeps_new_email_link(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            first=root/'abc';first.mkdir()
            source=shipping();source.update(id='abc',thread_id='abc',received_ms='1000')
            (first/'source.json').write_text(json.dumps(source))
            reader=Mock();reader.read.return_value={'status':'parsed','reader_version':2,'documents':[semantic()]}
            parse_pending(root,reader)
            second=root/'def';second.mkdir()
            source.update(id='def',thread_id='def',received_ms='2000')
            (second/'source.json').write_text(json.dumps(source))
            results=parse_pending(root,reader)
            reader.read.assert_called_once()
            self.assertTrue(results[1]['reused_identical_source'])
            self.assertEqual(results[1]['documents'][0]['thread_id'],'def')
            self.assertEqual(results[1]['documents'][0]['observed_ms'],'1000')

    def test_semantic_results_cached_and_unfamiliar_template_failure_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);folder=root/'abc';folder.mkdir()
            (folder/'source.json').write_text(json.dumps(shipping()))
            (folder/'order.json').write_text(json.dumps({'status':'needs_review','error':'unsupported_email_layout'}))
            reader=Mock();reader.read.return_value={'status':'parsed','reader_version':2,'documents':[semantic()]}
            self.assertEqual(parse_pending(root,reader)[0]['status'],'parsed')
            parse_pending(root,reader);reader.read.assert_called_once()

    def test_quota_keeps_all_messages_queued_and_does_not_fallback_to_template(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for mid in ['abc','def']:
                folder=root/mid;folder.mkdir();(folder/'source.json').write_text(json.dumps(shipping()))
            reader=Mock();reader.read.side_effect=ReaderError('codex_usage_limit')
            self.assertEqual([r['status'] for r in parse_pending(root,reader,clock=lambda:1000)],['waiting_usage']*2)
            parse_pending(root,reader,clock=lambda:1100);reader.read.assert_called_once()

    def test_unrelated_result_has_no_document_or_order_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);folder=root/'abc';folder.mkdir();(folder/'source.json').write_text(json.dumps(shipping()))
            reader=Mock();reader.read.return_value={'status':'ignored','reader_version':2}
            self.assertEqual(parse_pending(root,reader)[0]['status'],'ignored')


class EvidenceTests(unittest.TestCase):
    def test_identifier_punctuation_can_vary_but_digits_cannot_disappear(self):
        self.assertTrue(identifier_in_quote('14955237','Catalog: 14-955-237'))
        self.assertFalse(identifier_in_quote('123','Catalog 00123'))
        self.assertFalse(identifier_in_quote('00123','Catalog 123'))

    def test_fabricated_identifier_rejected_even_with_a_real_quote(self):
        # Build all optional values from the production schema, then supply the
        # smallest valid order-wide update to test the independent evidence gate.
        props=SCHEMA['properties']['documents']['items']['properties']
        doc={k:None for k in props}
        doc.update(supplier='Acme',order_id='FAKE',kind='order_status',status='unknown',items=[],
                   evidence=[{'field':'supplier','source':'email','quote':'Acme'},
                             {'field':'order_id','source':'email','quote':'Order A123'}])
        fields={'classification':'order_update','complete':True,'documents':[doc],'issues':[]}
        with self.assertRaisesRegex(ReaderError,'identifier_not_in_evidence'):
            validate_extraction(fields,{'email':'Acme Order A123'},[],[])
        doc['order_id']='A123';validate_extraction(fields,{'email':'Acme Order A123'},[],[])
        doc['evidence'][1]['quote']='Order B999'
        with self.assertRaisesRegex(ReaderError,'quote_not_in_source'):
            validate_extraction(fields,{'email':'Acme Order A123'},[],[])


class PackTests(unittest.TestCase):
    def test_unfamiliar_pack_wording_uses_semantic_factors_and_preserves_count_override(self):
        _,result=observed();item=result['fields']['items'][0]
        item['packaging_text']='ten sleeves, fifty tubes in every sleeve'
        item['pack_contents']={'outer_unit':'case','groups':10,'each_per_group':50,
                               'evidence':item['packaging_text']}
        self.assertEqual(infer_quantity(item)['quantity'],500)
        self.assertEqual(canonical(item['pack_contents']),'Case of 500')
        item['stated_quantity']={'count':4,'unit':'case','scope':'this_item','quote':'four cases'}
        self.assertEqual(infer_quantity(item,caption='four cases')['quantity'],2000)

    def test_conflicting_semantic_and_literal_pack_sizes_do_not_convert(self):
        _,result=observed();item=result['fields']['items'][0]
        item['pack_contents']={'outer_unit':'case','groups':10,'each_per_group':25,'evidence':'10PK/CS (50EA/PK)'}
        self.assertIsNone(infer_quantity(item))

    def test_bottle_volume_is_not_an_item_count(self):
        _,result=observed(unit='each');item=result['fields']['items'][0]
        item['packaging_text']='500 mL';item['pack_contents']=None
        self.assertEqual(infer_quantity(item)['quantity'],1)
