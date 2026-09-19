import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from inventory_sync import build_rows
from label_reader import slack_payload
from label_worker import LabelWorker
from photo_intake import write_json
from product_matching import receipt_order_match
from receipt_reply import build_reply, live_orders
from sheet_sync import receipt_rows
from test_receipt_quantity import observed


def context():
    record,result=observed(); record.update(caption='',status='awaiting_ai',thread_ts=record['message_ts'])
    r=receipt_rows(record,result)[0]; r[8:10]=[500,'each']
    o=['A123',46000,'Requester','Fisher Scientific','Supplier full tube name','00123','15 mL; Case of 500',3,'case','Shipped 3 of 3','TRACK1','email','PO: PO123']
    inventory={5:next(iter(build_rows([o],[r]).values()))}
    config={'spreadsheet_id':'test-sheet','tabs':{name:{'sheet_id':i} for i,name in enumerate(['Orders','Inventory','Package receipts'],1)}}
    return record,result,{5:r},live_orders({5:o}),inventory,config


class ReplyTests(unittest.TestCase):
    def test_live_product_title_provisional_order_totals_and_safe_buttons(self):
        record,result,receipts,orders,inv,config=context()
        reply=build_reply(result,record,orders,inv,receipts,config)
        self.assertIn('Received: Supplier full tube name',reply['text'])
        self.assertIn('Possible order: A123',reply['text'])
        self.assertIn('1500 ordered / 500 received / 1000 outstanding',reply['text'])
        self.assertEqual(len(reply['links']),3)
        payload=slack_payload(reply['text']+' <!channel>',reply['links']+[{'text':'Bad','url':'https://evil.invalid'}])
        self.assertEqual(len(payload['blocks'][-1]['elements']),3)
        self.assertFalse(payload['mrkdwn']);self.assertNotIn('<!channel>',payload['text'])

    def test_exact_po_tracking_and_conflicting_references(self):
        _,_,receipts,orders,_,_=context();r=receipts[5]
        r[15]='PO123'; self.assertEqual(receipt_order_match(r,orders)['confirmed_order'],'A123')
        r[15]='';r[14]='TRACK1';self.assertEqual(receipt_order_match(r,orders)['confirmed_order'],'A123')
        r[15]='A123';r[14]='OTHER';self.assertEqual(receipt_order_match(r,orders)['candidates'],[])

    def test_spec_conflicts_wrong_supplier_and_multiple_possible_orders(self):
        record,result,receipts,orders,inv,config=context();r=receipts[5]
        r[6]='50 mL'
        reply=build_reply(result,record,orders,inv,receipts,config)
        self.assertIn('Specifications conflict',reply['text'])
        self.assertNotIn('Inventory delivery totals',reply['text'])
        r[6]='15 mL';r[4]='Other supplier'
        self.assertEqual(receipt_order_match(r,orders)['candidates'],[])
        r[4]='Fisher Scientific'
        other=copy.deepcopy(next(iter(orders.values())));other['order_id']='A456';orders['other']=other
        reply=build_reply(result,record,orders,inv,receipts,config)
        self.assertIn('Possible orders: A123, A456',reply['text'])
        self.assertNotIn('Matched order:',reply['text'])

    def test_sheet_failure_retries_context_without_rereading_image_or_posting_twice(self):
        record,result,receipts,orders,inv,config=context()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); folder=root/'message';folder.mkdir();manifest=folder/'record.json'
            write_json(manifest,record)
            reader=Mock();reader.read.return_value=result
            client=Mock();client.chat_postMessage.return_value={'ts':'200.1'}
            builder=Mock();builder.prepare.side_effect=[TimeoutError(),build_reply(result,record,orders,inv,receipts,config)]
            now=[1000]
            worker=LabelWorker(root,'C123',client,reader,clock=lambda:now[0],receipt_reply=builder)
            self.assertEqual(worker.process(manifest)['status'],'ready')
            client.chat_postMessage.assert_not_called()
            now[0]+=61
            self.assertEqual(worker.process(manifest)['status'],'sent')
            worker.process(manifest)
            reader.read.assert_called_once();client.chat_postMessage.assert_called_once()
            self.assertIn('Possible order',client.chat_postMessage.call_args.kwargs['text'])
            saved=json.loads(manifest.with_name('analysis.json').read_text())
            self.assertEqual(saved['receipt_context']['matches'][0]['match']['candidates'],['A123'])
