import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from label_reader import ReaderError
from query_analyst import QueryAnalyst
from query_data import QueryDatabase, snapshot, fingerprint, ReadOnlyOrders, ReadOnlyInventory, ReadOnlyReceipts
from test_status_queries import context, WorkerTests


def planning(action='query',sql='SELECT source_ref, product, received FROM inventory',clarification=None):
    return {'fields':{'action':action,'queries':[{'purpose':'Read inventory','sql':sql}] if action=='query' else [],
                      'clarification':clarification}}


def answering(text='Recorded deliveries: 500 each.',refs=None):
    return {'fields':{'text':text,'source_refs':refs if refs is not None else ['inventory:5']}}


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.data=snapshot(*context())
        self.db=QueryDatabase(self.data)
        self.addCleanup(self.db.close)

    def test_flexible_group_comparison_and_date_queries(self):
        result=self.db.execute('SELECT requester, COUNT(DISTINCT order_id) AS n, SUM(ordered_each) AS items FROM orders GROUP BY requester')
        self.assertEqual(result['rows'][0][1:],[1,1500])
        result=self.db.execute('SELECT received, outstanding, ROUND(100.0*received/ordered,2) AS received_percent FROM inventory')
        self.assertEqual(result['rows'],[[500,1000,33.33]])
        self.assertEqual(self.db.execute("SELECT count(*) AS n FROM orders WHERE order_date >= '2100-01-01'")['rows'],[[0]])

    def test_sql_writes_files_metadata_and_extensions_are_denied(self):
        for sql in ["DELETE FROM inventory","UPDATE inventory SET received=0","DROP TABLE orders",
                    "CREATE TABLE x(a)","ATTACH DATABASE ':memory:' AS other","PRAGMA table_info(orders)",
                    "SELECT load_extension('bad')","SELECT * FROM sqlite_master",
                    "SELECT 1; DELETE FROM orders"]:
            with self.subTest(sql=sql),self.assertRaises(ReaderError):self.db.execute(sql)
        self.assertEqual(self.db.execute('SELECT received FROM inventory')['rows'],[[500]])

    def test_large_or_recursive_queries_are_bounded(self):
        with self.assertRaises(ReaderError):
            self.db.execute('WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n')
        result=self.db.execute('WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<250) SELECT x FROM n')
        self.assertTrue(result['truncated']);self.assertEqual(len(result['rows']),200)
        with self.assertRaises(ReaderError):self.db.execute('SELECT 1 AS x, 2 AS x')

    def test_snapshot_all_facts_change_cache_key_and_unknowns_stay_null(self):
        changed=copy.deepcopy(self.data);changed['tables']['inventory'][0]['received']=750
        self.assertNotEqual(fingerprint(changed),fingerprint(self.data))
        self.assertIsNone(self.data['tables']['receipts'][0]['confirmed_order_id'])
        self.assertEqual(self.data['tables']['receipts'][0]['received_each'],500)

    def test_email_details_available_without_double_counting_order_table(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)/'message';folder.mkdir()
            document={'supplier':'Fisher','order_id':'A123','kind':'invoice_notification',
                      'items':[{'catalog':'00123','unit_price':'68.90','quantity_shipped':3}]}
            (folder/'order.json').write_text(json.dumps({'status':'parsed','document':document}))
            data=snapshot(*context(),email_root=Path(temp))
            db=QueryDatabase(data)
            try:
                result=db.execute("SELECT json_extract(details,'$.item.unit_price'), currency FROM email_events")
                self.assertEqual(result['rows'],[['68.90',None]])
                self.assertEqual(db.execute('SELECT COUNT(*) FROM orders')['rows'],[[1]])
            finally:db.close()

    def test_google_query_clients_reject_writes_before_network(self):
        config=context()[3]
        for cls in (ReadOnlyOrders,ReadOnlyInventory,ReadOnlyReceipts):
            store=cls(config)
            with self.assertRaisesRegex(ValueError,'query_write_forbidden'):
                store.request('POST','https://sheets.googleapis.com/example')
            self.assertIsNone(store.session)


class AnalystTests(unittest.TestCase):
    def setUp(self):
        self.reader=Mock();self.analyst=QueryAnalyst(self.reader);self.data=snapshot(*context())

    def test_analysis_reads_evidence_before_answer_and_builds_verified_links(self):
        self.reader.structured.side_effect=[planning(),planning('answer'),answering()]
        result=self.analyst.analyze('What have we got?',self.data,1800000000)
        self.assertEqual(len(result['reply']['links']),1)
        self.assertIn('500',result['reply']['text'])
        self.assertIn('previous_results',json.loads(self.reader.structured.call_args_list[1].args[2]))

    def test_adaptive_lookup_and_sql_error_repair(self):
        self.reader.structured.side_effect=[planning(sql='SELECT missing FROM orders'),
            planning(sql='SELECT product FROM inventory'),planning(),planning('answer'),answering()]
        result=self.analyst.analyze('Tell me about those supplies',self.data,1800000000)
        self.assertEqual(len(result['evidence']),3)
        self.assertIn('error',result['evidence'][0])

    def test_irrelevant_and_true_missing_identity_do_not_invent_facts(self):
        for plan in [planning('ignore'),planning('clarify',clarification='Which requester name should I use?')]:
            self.reader.structured.return_value=plan
            result=self.analyst.analyze('test',self.data,1800000000)
            self.assertEqual(result['reply'] is None,plan['fields']['action']=='ignore')

    def test_unverified_sources_and_external_urls_are_rejected(self):
        for answer in [answering(refs=['inventory:999']),answering(refs=['orders:5']),
                       answering('Read https://example.invalid')]:
            self.reader.structured.side_effect=[planning(),planning('answer'),answer]
            with self.assertRaises(ReaderError):self.analyst.analyze('test',self.data,1800000000)

    def test_quota_error_is_not_replaced_by_keyword_fallback(self):
        self.reader.structured.side_effect=ReaderError('codex_usage_limit')
        with self.assertRaisesRegex(ReaderError,'codex_usage_limit'):
            self.analyst.analyze('test',self.data,1800000000)


class AnalystWorkerTests(WorkerTests):
    def test_same_snapshot_retry_reuses_answer_without_repeated_prefix(self):
        class Rejected(Exception): response={'ok':False,'error':'ratelimited'}
        self.worker.reply_prefix='TEST MODE\n'
        self.client.chat_postMessage.side_effect=Rejected()
        self.worker.capture(self.event);self.worker.process(self.path())
        self.now+=61;self.client.chat_postMessage.side_effect=None
        self.worker.process(self.path())
        self.interpreter.analyze.assert_called_once()
        self.assertEqual(self.client.chat_postMessage.call_args.kwargs['text'].count('TEST MODE'),1)
