import copy
import unittest

from label_reader import ReaderError
from query_semantics import validate_plan, catalog
from status_queries import answer_query
import test_status_queries
from test_status_queries import context


def plan(**changes):
    return dict({'action':'inventory', 'selection':'all', 'record_keys':[],
                 'period':None, 'wants_eta':False, 'clarification':None}, **changes)


class PlanTests(unittest.TestCase):
    def test_order_dates_filter_orders_not_recent_receipts(self):
        from datetime import datetime, timezone
        orders, inventory, receipts, config = context()
        orders[5][1] = '2026-09-14'
        for row, order_id, date in [(6,'OLD','2026-09-13'),(7,'END','2026-09-21'),
                                     (8,'MISSING',''),(9,'BAD','not a date')]:
            orders[row] = copy.deepcopy(orders[5])
            orders[row][0], orders[row][1] = order_id, date
        receipts[5][1] = 25569  # Receipt date must not affect order selection.
        period = {'start':'2026-09-14T00:00:00Z','end':'2026-09-21T00:00:00Z'}
        reply = answer_query('what did we buy this week',orders,inventory,receipts,config,
                             now=datetime(2026,9,19,tzinfo=timezone.utc).timestamp(),
                             plan=plan(action='orders',period=period))
        self.assertIn('order A123',reply['text'])
        self.assertIn('Order date: 2026-09-14',reply['text'])
        self.assertIn('2 matching order line(s)',reply['text'])
        for identifier in ('OLD','END','MISSING','BAD'):
            self.assertNotIn('order '+identifier,reply['text'])

    def test_empty_order_period_answers_directly_and_keeps_date_limit(self):
        orders, inventory, receipts, config = context()
        orders[5][1] = '2026-09-10'
        reply = answer_query('orders placed',orders,inventory,receipts,config,now=1800000000,
                             plan=plan(action='orders',period={
                                 'start':'2026-09-14T00:00:00Z','end':'2026-09-20T00:00:00Z'}))
        self.assertIn('No matching orders with recorded order dates',reply['text'])
        self.assertIn('2026-09-14',reply['text'])
        self.assertEqual(reply['links'],[])

    def test_native_sheet_date_and_invalid_dates(self):
        from status_queries import order_date
        self.assertEqual(order_date(46279).strftime('%Y-%m-%d'),'2026-09-14')
        for value in ('',None,True,float('nan'),float('inf'),'09/10/26','2026-99-99'):
            self.assertIsNone(order_date(value))

    def test_order_response_preserves_uncertainty_and_multiple_matches(self):
        orders, inventory, receipts, config = context()
        second = copy.deepcopy(orders[5]);second[0]='A456';orders[6]=second
        reply=answer_query('any ETA on tubes?',orders,inventory,receipts,config,
                           plan=plan(action='orders',selection='matches',
                                     record_keys=['orders:5','orders:6'],wants_eta=True))
        for phrase in ['A123','A456','No delivery ETA is recorded',
                       'No quantified lab receipt is confirmed','not order-specific']:
            self.assertIn(phrase,reply['text'])

    def test_executor_cannot_fallback_to_keyword_parser(self):
        with self.assertRaisesRegex(ReaderError,'missing_query_plan'):
            answer_query('show your inventory',*context())

    def test_executor_uses_plan_not_request_words(self):
        reply = answer_query('gimme a rundown', *context(), plan=plan())
        self.assertIn('Basix', reply['text'])
        self.assertIn('500', reply['text'])
        reply = answer_query('any word on the conicals?', *context(),
                             plan=plan(action='orders', selection='matches', record_keys=['orders:5']))
        self.assertIn('A123', reply['text'])

    def test_impossible_cross_tab_or_unknown_keys_and_bad_dates_fail_closed(self):
        records = catalog(*context()[:3])
        bad = [plan(selection='matches', record_keys=['inventory:999']),
               plan(selection='matches', record_keys=['orders:5']),
               plan(selection='matches', record_keys=['inventory:5', 'inventory:5']),
               plan(selection='all', record_keys=['inventory:5']),
               plan(period={'start':'2026-01-01', 'end':'2026-02-01'}),
               plan(period={'start':'2027-01-01T00:00:00Z', 'end':'2026-01-01T00:00:00Z'}),
               plan(action='ignore', period={'start':'2026-01-01T00:00:00Z', 'end':'2026-02-01T00:00:00Z'})]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ReaderError):
                validate_plan(value, records, 1800000000)

    def test_date_window_controls_totals_without_keyword_matching(self):
        orders, inventory, receipts, config = context()
        now = 1800000000
        receipts[5][1] = 25569 + (now-86400)/86400
        old = copy.deepcopy(receipts[5]);old[0]='old';old[16]='old-link'
        old[1] = 25569 + (now-9*86400)/86400; old[8]=1000;receipts[6]=old
        from datetime import datetime, timezone
        period={k:datetime.fromtimestamp(t,timezone.utc).isoformat()
                for k,t in [('start',now-7*86400),('end',now)]}
        reply=answer_query('just the recent additions please',orders,inventory,receipts,config,
                           now=now,plan=plan(period=period))
        self.assertIn('Received in this period: 500 each', reply['text'])
        self.assertNotIn('1500', reply['text'])


class SemanticWorkerTests(test_status_queries.WorkerTests):
    def test_diverse_text_is_not_gated_by_keywords(self):
        self.event['text']='gimme a rundown'
        self.assertTrue(self.worker.capture(self.event))
        self.assertEqual(self.worker.process(self.path())['status'], 'sent')
        self.interpreter.analyze.assert_called_once()

    def test_quota_queues_without_wrong_answer_or_keyword_fallback(self):
        self.interpreter.analyze.side_effect=ReaderError('codex_usage_limit')
        self.worker.capture(self.event)
        state=self.worker.process(self.path())
        self.assertEqual(state['status'],'waiting_usage')
        self.client.chat_postMessage.assert_not_called()
        self.now+=61;self.worker.process(self.path())
        self.interpreter.analyze.assert_called_once()

    def test_retry_reuses_plan_and_refreshes_live_quantities(self):
        class Rejected(Exception):
            response={'ok':False,'error':'ratelimited'}
        self.client.chat_postMessage.side_effect=Rejected()
        self.worker.capture(self.event)
        self.assertEqual(self.worker.process(self.path())['status'],'retry_wait')
        self.now+=61
        self.worker.inventory.snapshot.return_value[5][5]=750
        self.client.chat_postMessage.side_effect=None
        self.assertEqual(self.worker.process(self.path())['status'],'sent')
        self.assertEqual(self.interpreter.analyze.call_count,2)
        self.assertIn('750 received', self.client.chat_postMessage.call_args.kwargs['text'])

    def test_thread_followup_sees_earlier_subject(self):
        self.worker.capture(self.event);self.worker.process(self.path())
        self.event.update(ts='101.1',text='just the ones added this week')
        self.worker.capture(self.event)
        self.worker.process(self.worker.root/'C123_101.1.json')
        context_messages=self.interpreter.analyze.call_args.args[3]
        self.assertEqual(context_messages[0]['message'],'How many tubes do we have?')
