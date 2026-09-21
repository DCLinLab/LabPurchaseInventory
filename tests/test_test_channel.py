import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from google_auth import EXPECTED_EMAIL
from photo_intake import write_json
from slack_bot import Settings
from test_channel import (PreviewReceiptReply, ReadOnlyOrders, ReadOnlyInventory,
                          ReadOnlyReceipts, configured_test_channels, start_test_channel)
from test_receipt_reply import context


class TestChannelTests(unittest.TestCase):
    def fixtures(self, temp):
        record, result, receipts, orders, inventory, config = context()
        config['account'] = EXPECTED_EMAIL
        root = Path(temp)
        folder = root / 'test-intake-C123' / ('C123_' + record['message_ts'])
        folder.mkdir(parents=True)
        manifest = folder / 'record.json'
        write_json(manifest, record)
        write_json(folder / 'analysis.json', {'result': result})
        builder = PreviewReceiptReply(root, config)
        order = ['A123',46000,'Requester','Fisher Scientific','Supplier full tube name','00123',
                 '15 mL; Case of 500',3,'case','Shipped 3 of 3','TRACK1','email','PO: PO123']
        builder.orders = Mock(spec=['snapshot'])
        builder.inventory = Mock(spec=['snapshot'])
        builder.orders.snapshot.return_value = {5: order}
        builder.inventory.snapshot.return_value = inventory
        return record, result, manifest, builder, config

    def test_preview_uses_live_context_without_writes_or_fake_receipt_link(self):
        with tempfile.TemporaryDirectory() as temp:
            record, result, manifest, builder, _ = self.fixtures(temp)
            before = copy.deepcopy(builder.inventory.snapshot.return_value)
            with patch('receipt_reply.ReceiptSync', side_effect=AssertionError('production sync invoked')):
                reply = builder.prepare(result, record, manifest)
            self.assertIn('TEST MODE', reply['text'])
            self.assertIn('Test receipt: Supplier full tube name', reply['text'])
            self.assertIn('Would record: 500 each', reply['text'])
            self.assertIn('Possible order: A123', reply['text'])
            self.assertIn('excludes this test', reply['text'])
            self.assertNotIn('Open receipt', [x['text'] for x in reply['links']])
            self.assertEqual(before, builder.inventory.snapshot.return_value)

    def test_explicit_count_and_duplicate_photo_use_production_quantity_rules(self):
        with tempfile.TemporaryDirectory() as temp:
            record, result, manifest, builder, _ = self.fixtures(temp)
            record['caption'] = 'Received 3 cases'
            result['fields']['items'][0]['stated_quantity'] = {
                'count':3, 'unit':'case', 'scope':'this_item', 'quote':'Received 3 cases'}
            write_json(manifest, record)
            write_json(manifest.with_name('analysis.json'), {'result':result})
            self.assertIn('Would record: 1500 each', builder.prepare(result,record,manifest)['text'])
            record['message_ts'] = '9999999999.000001'
            folder = manifest.parent.parent / ('C123_' + record['message_ts'])
            folder.mkdir()
            write_json(folder/'record.json', record)
            write_json(folder/'analysis.json', {'result':result})
            reply = builder.prepare(result,record,folder/'record.json')
            self.assertNotIn('Would record:', reply['text'])

    def test_test_sheet_clients_reject_writes_before_network(self):
        with tempfile.TemporaryDirectory() as temp:
            *_, config = self.fixtures(temp)
            for cls in (ReadOnlyOrders, ReadOnlyInventory, ReadOnlyReceipts):
                store = cls(config)
                for method in ('POST','PUT','PATCH','DELETE'):
                    with self.assertRaisesRegex(ValueError, 'test_channel_write_forbidden'):
                        store.request(method, 'https://sheets.googleapis.com/example')
                self.assertIsNone(store.session)

    def test_configuration_rejects_production_and_invalid_channels(self):
        for value in ('CMAIN','bad-channel','C123,CMAIN'):
            with patch.dict(os.environ, {'SLACK_TEST_CHANNEL_IDS':value}):
                with self.assertRaises(ValueError): configured_test_channels('CMAIN')
        with patch.dict(os.environ, {'SLACK_TEST_CHANNEL_IDS':'C123, C456 C123'}):
            self.assertEqual(configured_test_channels('CMAIN'), ['C123','C456'])

    def test_routing_and_queues_are_separate_and_only_readers_start(self):
        with tempfile.TemporaryDirectory() as temp:
            *_, config = self.fixtures(temp)
            settings = Settings('xoxb-test','xapp-test','T123','A123','CMAIN')
            client = Mock()
            with patch('test_channel.StatusQueryWorker.start'), patch('test_channel.LabelWorker.start'):
                receiver, workers = start_test_channel(temp,settings,'C123','UBOT',client,config,True)
            self.assertEqual(len(workers),2)
            self.assertEqual(receiver.photo_intake.root.name,'test-intake-C123')
            self.assertEqual(receiver.query_worker.root.name,'test-queries-C123')
            self.assertIsInstance(workers[1].receipt_reply, PreviewReceiptReply)
            receiver.query_worker.capture = Mock(return_value=False)
            body={'team_id':'T123','api_app_id':'A123','event':{
                'type':'message','user':'UHUMAN','channel':'CMAIN','ts':'100.1','text':'ping'}}
            receiver.receive(body,client)
            client.chat_postMessage.assert_not_called()
            body['event']['channel']='C123'
            receiver.receive(body,client)
            self.assertIn('TEST MODE',client.chat_postMessage.call_args.kwargs['text'])
            self.assertEqual(client.chat_postMessage.call_args.kwargs['channel'],'C123')
