import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from order_email import parse_fisher
from order_sync import merge_documents, order_row
from order_notifications import OrderNotifier, status_fingerprint, status_text
from test_order_email import shipping, invoice


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.client=Mock();self.client.chat_postMessage.return_value={'ts':'123.45'}
        self.config={'spreadsheet_id':'test','tabs':{'Orders':{'sheet_id':1}}}
        self.notifier=OrderNotifier(Path(self.temp.name)/'notices.json',self.client,'Ctarget',self.config)
        self.orders,_=merge_documents([parse_fisher(shipping())]);self.key=next(iter(self.orders))
        self.store=Mock(); self.refresh()

    def refresh(self):
        self.store.snapshot.return_value={5:order_row(self.orders[self.key])}
        self.statuses={self.key:'synced'}

    def test_new_order_reports_once_across_restarts_and_duplicate_forwards(self):
        self.assertEqual(self.notifier.run(self.orders,self.statuses,self.store)['sent'],1)
        self.assertEqual(self.client.chat_postMessage.call_args.kwargs['channel'],'Ctarget')
        self.assertNotIn('thread_ts',self.client.chat_postMessage.call_args.kwargs)
        self.orders[self.key]['message_ids'].append('new-forward-id')
        self.orders[self.key]['sources'].append('another-mail-link');self.refresh()
        restarted=OrderNotifier(self.notifier.path,self.client,'Ctarget',self.config)
        self.assertEqual(restarted.run(self.orders,self.statuses,self.store)['sent'],0)
        self.client.chat_postMessage.assert_called_once()

    def test_baseline_and_later_invoice_and_tracking_changes_stay_silent(self):
        self.notifier.baseline(self.orders)
        self.notifier.run(self.orders,self.statuses,self.store)
        self.orders,_=merge_documents([parse_fisher(shipping()),parse_fisher(invoice())]); self.refresh()
        self.assertEqual(self.notifier.run(self.orders,self.statuses,self.store)['sent'],0)
        self.orders[self.key]['tracking']=['T123'];self.refresh()
        self.assertEqual(self.notifier.run(self.orders,self.statuses,self.store)['sent'],0)
        self.client.chat_postMessage.assert_not_called()

    def test_later_status_and_new_catalog_line_do_not_reannounce_order(self):
        self.notifier.run(self.orders,self.statuses,self.store)
        self.orders[self.key]['tracking']=['NEW']
        other=copy.deepcopy(self.orders[self.key]);other['catalog']='ANOTHER'
        from order_sync import order_key
        key=order_key(other['supplier'],other['order_id'],other['catalog'])
        self.orders[key]=other
        self.statuses[key]='synced'
        restarted=OrderNotifier(self.notifier.path,self.client,'Ctarget',self.config)
        self.assertEqual(restarted.run(self.orders,self.statuses,self.store)['sent'],0)
        self.client.chat_postMessage.assert_called_once()
        self.assertEqual(restarted.load()['orders'][self.key],status_fingerprint(self.orders[self.key]))

    def test_multiple_lines_in_new_order_make_one_post(self):
        other=copy.deepcopy(self.orders[self.key]);other['catalog']='ANOTHER'
        from order_sync import order_key
        key=order_key(other['supplier'],other['order_id'],other['catalog'])
        self.orders[key]=other;self.statuses[key]='synced'
        self.store.snapshot.return_value[6]=order_row(other)
        self.assertEqual(self.notifier.run(self.orders,self.statuses,self.store)['sent'],1)
        self.client.chat_postMessage.assert_called_once()
        self.assertIn('ANOTHER',self.client.chat_postMessage.call_args.kwargs['text'])

    def test_unsynced_or_manually_changed_order_does_not_announce_wrong_status(self):
        self.notifier.run(self.orders,{self.key:'needs_review'},self.store)
        self.client.chat_postMessage.assert_not_called()
        self.store.snapshot.return_value[5][9]='Edited'
        self.assertEqual(self.notifier.run(self.orders,self.statuses,self.store)['needs_review'],1)
        self.client.chat_postMessage.assert_not_called()

    def test_lost_slack_response_is_not_resent_after_restart(self):
        self.client.chat_postMessage.side_effect=TimeoutError()
        self.assertEqual(self.notifier.run(self.orders,self.statuses,self.store)['delivery_uncertain'],1)
        self.notifier.run(self.orders,self.statuses,self.store)
        self.client.chat_postMessage.assert_called_once()

    def test_crash_after_posting_journal_and_before_response_is_not_resent(self):
        import hashlib
        eid=hashlib.sha256((self.key+'|'+status_fingerprint(self.orders[self.key])).encode()).hexdigest()
        journal=self.notifier.load();journal['events'][eid]={'status':'posting'}
        self.notifier.path.write_text(json.dumps(journal))
        self.notifier.run(self.orders,self.statuses,self.store)
        self.client.chat_postMessage.assert_not_called()
        self.assertEqual(self.notifier.load()['events'][eid]['status'],'delivery_uncertain')

    def test_unidentified_review_emails_stay_silent(self):
        reviews=[{'message_id':'abc','subject':'Order update <!channel> click bad link'}]
        self.assertEqual(self.notifier.run({}, {},self.store,reviews)['needs_review'],1)
        self.client.chat_postMessage.assert_not_called()

    def test_status_post_does_not_claim_lab_receipt_or_include_price(self):
        text=status_text(self.orders[self.key])
        self.assertIn('Shipped: 3 of 3 case',text)
        self.assertIn('Lab receipt is tracked separately',text)
        self.assertNotIn('68.90',text)


class NotificationRoutingTests(unittest.TestCase):
    def configured(self, destination, enabled=True):
        from order_sync import configured_order_worker
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / '.local').mkdir()
        (root / '.local/email-config.json').write_text(json.dumps({
            'enabled': True, 'slack_notifications_enabled': enabled,
            'slack_notification_channel_id': destination}))
        (root / '.local/google-sheet.json').write_text(json.dumps({'spreadsheet_id': 'test'}))
        with patch('order_sync.ROOT', root), patch('order_sync.OrderWorker') as worker:
            configured_order_worker(Mock(), 'C04A5S6B7GX')
            return worker.call_args.args[2]

    def test_email_reports_route_only_to_production_channel(self):
        notifier = self.configured('C04A5S6B7GX')
        self.assertEqual(notifier.channel_id, 'C04A5S6B7GX')
        self.assertEqual(notifier.path.name, 'slack-notifications-C04A5S6B7GX.json')

    def test_missing_or_test_destination_fails_closed(self):
        for destination in (None, 'C0C353SN0D7'):
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(ValueError, 'require_production_channel'):
                    self.configured(destination)

    def test_disabled_notifications_remain_silent(self):
        self.assertIsNone(self.configured(None, enabled=False))


class ReportFormattingTests(unittest.TestCase):
    def test_readable_report_and_email_html_are_safely_formatted(self):
        from order_notifications import order_payload
        order={'product':'Medchemexpress, L-DOPA','supplier':'Fisher Scientific',
               'order_id':'A62679209','catalog':'50-193-3514','quantity_ordered':1,
               'unit':'each','quantity_shipped':None,'status':'delayed',
               'status_detail':'Estimated delivery date: 10/02/2026',
               'specifications':'200mg; CAS:59-92-7; Purity:&gt;98%; &#x20;',
               'pack_size':'Each of 1','shipments':[]}
        text=status_text(order)
        self.assertIn('*Order update — L-DOPA*',text)
        self.assertIn('• *Quantity:* 1 × 200 mg',text)
        self.assertIn('October 2, 2026',text)
        self.assertNotIn('&#x20;',text)
        payload=order_payload(text,[])
        self.assertEqual(payload['blocks'][0]['text']['type'],'mrkdwn')
        self.assertTrue(payload['blocks'][0]['text']['verbatim'])
        order['product']='<!channel> *spoof*'
        text=status_text(order)
        self.assertNotIn('<!channel>',text)
        self.assertNotIn('*spoof*',text)
