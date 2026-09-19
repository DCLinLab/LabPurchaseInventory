import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from inventory_sync import build_rows
from status_queries import answer_query, StatusQueryWorker
from test_inventory_sync import sources


def context():
    order,receipt=sources()
    order[4]='Basix Centrifuge Tubes'
    inventory={5:next(iter(build_rows([order],[receipt]).values()))}
    config={'account':'linjhumse@gmail.com','spreadsheet_id':'test',
            'tabs':{name:{'sheet_id':i} for i,name in enumerate(('Orders','Inventory','Package receipts'),1)}}
    return {5:order},inventory,{5:receipt},config


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        orders,inventory,receipts,config=context()
        self.client=Mock();self.client.chat_postMessage.return_value={'ts':'200.1'}
        self.now=1000
        self.interpreter=Mock()
        self.interpreter.interpret.return_value={'fields': {'action':'inventory','selection':'all','record_keys':[], 'period':None,'wants_eta':False,'clarification':None}}
        self.worker=StatusQueryWorker(Path(self.temp.name)/'queries','C123',self.client,config,clock=lambda:self.now,interpreter=self.interpreter)
        for name,rows in [('orders',orders),('inventory',inventory),('receipts',receipts)]:
            store=Mock();store.snapshot.return_value=rows;setattr(self.worker,name,store)
        self.event={'channel':'C123','ts':'100.1','thread_ts':'90.1','text':'How many tubes do we have?'}

    def path(self):return next(self.worker.root.glob('*.json'))

    def test_query_replies_in_thread_once_and_never_writes_google(self):
        self.assertTrue(self.worker.capture(self.event));self.worker.capture(self.event)
        self.assertEqual(self.worker.process(self.path())['status'],'sent')
        self.worker.process(self.path())
        self.client.chat_postMessage.assert_called_once()
        self.assertEqual(self.client.chat_postMessage.call_args.kwargs['thread_ts'],'90.1')
        for store in (self.worker.orders,self.worker.inventory,self.worker.receipts):
            self.assertEqual([x[0] for x in store.mock_calls],['snapshot'])

    def test_sheet_error_retries_but_uncertain_slack_delivery_does_not(self):
        self.worker.capture(self.event); self.worker.orders.snapshot.side_effect=TimeoutError()
        self.assertEqual(self.worker.process(self.path())['status'],'retry_wait')
        self.client.chat_postMessage.assert_not_called()
        self.now+=61;self.worker.orders.snapshot.side_effect=None
        self.client.chat_postMessage.side_effect=TimeoutError()
        self.assertEqual(self.worker.process(self.path())['status'],'delivery_uncertain')
        self.worker.process(self.path());self.client.chat_postMessage.assert_called_once()

    def test_irrelevant_query_silent_and_arrival_photo_still_goes_to_intake(self):
        self.interpreter.interpret.return_value['fields']['action']='ignore'
        self.event['text']='Where should we eat lunch?';self.worker.capture(self.event)
        self.assertEqual(self.worker.process(self.path())['status'],'ignored')
        self.client.chat_postMessage.assert_not_called()
        self.event.update(ts='101.1',files=[{'id':'F1'}],text='Received 4 cases. Where should these be stored?')
        self.assertFalse(self.worker.capture(self.event))

    def test_restarted_posting_state_is_not_repeated(self):
        self.worker.capture(self.event)
        state=json.loads(self.path().read_text());state['status']='posting'
        self.path().write_text(json.dumps(state))
        self.assertEqual(self.worker.process(self.path())['status'],'delivery_uncertain')
        self.client.chat_postMessage.assert_not_called()
