import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

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

    def test_baseline_is_quiet_and_later_invoice_and_tracking_changes_report(self):
        self.notifier.baseline(self.orders)
        self.notifier.run(self.orders,self.statuses,self.store)
        self.client.chat_postMessage.assert_not_called()
        self.orders,_=merge_documents([parse_fisher(shipping()),parse_fisher(invoice())]); self.refresh()
        self.assertEqual(self.notifier.run(self.orders,self.statuses,self.store)['sent'],1)
        self.assertIn('not a bill to pay',self.client.chat_postMessage.call_args.kwargs['text'])
        self.orders[self.key]['tracking']=['T123'];self.refresh()
        self.assertEqual(self.notifier.run(self.orders,self.statuses,self.store)['sent'],1)

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

    def test_unsupported_email_notice_once_and_no_email_instructions_execute(self):
        reviews=[{'message_id':'abc','subject':'Order update <!channel> click bad link'}]
        self.notifier.run({}, {},self.store,reviews)
        self.notifier.run({}, {},self.store,reviews)
        self.client.chat_postMessage.assert_called_once()
        payload=self.client.chat_postMessage.call_args.kwargs
        self.assertFalse(payload['mrkdwn']);self.assertNotIn('<!channel>',payload['text'])
        self.assertIn('could not reliably extract',payload['text'])

    def test_status_post_does_not_claim_lab_receipt_or_include_price(self):
        text=status_text(self.orders[self.key])
        self.assertIn('Shipped: 3 of 3 case',text)
        self.assertIn('lab receipt is tracked separately',text)
        self.assertNotIn('68.90',text)
