import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from label_reader import render_reply, validate_result
from label_worker import LabelWorker
from message_intent import is_delivery_item
from photo_intake import write_json
from receipt_quantity import infer_quantity, quantity_candidates
from sheet_sync import ReceiptSync, receipt_rows
from test_receipt_quantity import observed
from test_sheet_sync import FakeStore


class VisualIntentTests(unittest.TestCase):
    def test_new_semantic_delivery_can_override_old_shortage_vocabulary(self):
        record, result = observed()
        record['caption'] = 'Running out was a problem. The replenishment is here now.'
        result['semantic_assessment_version'] = 4
        self.assertEqual(len(receipt_rows(record, result)), 1)
        self.assertIn('Inferred received:', render_reply(result, record))
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'message';folder.mkdir()
            write_json(folder/'record.json',record)
            write_json(folder/'analysis.json',{'status':'ready','result':result})
            self.assertEqual(len(quantity_candidates(Path(temp))),1)

    def test_readable_non_delivery_cannot_reply_create_receipt_or_infer_quantity(self):
        for kind, confidence in [('existing_supply','high'), ('uncertain','high'),
                                 ('delivery','medium'), ('unrelated','high')]:
            record, result = observed()
            item = result['fields']['items'][0]
            item['receipt_assessment'].update(kind=kind, confidence=confidence)
            self.assertFalse(is_delivery_item(item))
            self.assertIsNone(render_reply(result, record))
            self.assertEqual(receipt_rows(record, result), [])
            self.assertIsNone(infer_quantity(item))

    def test_legacy_or_evidence_free_label_cannot_be_new_delivery(self):
        record, result = observed()
        item = result['fields']['items'][0]
        del item['receipt_assessment']
        validate_result(result['fields'], ['F123'])  # Can still inspect historical extraction.
        self.assertEqual(receipt_rows(record, result), [])
        self.assertIsNone(infer_quantity(item))
        item['receipt_assessment'] = {'kind':'delivery','confidence':'high','evidence':None}
        self.assertFalse(is_delivery_item(item))

    def test_mixed_scene_counts_only_delivery_and_preserves_item_ids(self):
        record, result = observed()
        valid_id = receipt_rows(record, result)[0][0]
        background = copy.deepcopy(result['fields']['items'][0])
        background['product'] = 'Used bench bottle'
        background['receipt_assessment']['kind'] = 'existing_supply'
        result['fields']['items'].append(background)
        rows = receipt_rows(record, result)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], valid_id)
        self.assertNotIn('Used bench bottle', render_reply(result,record))
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)/'message'; folder.mkdir()
            write_json(folder/'record.json',record)
            write_json(folder/'analysis.json',{'status':'ready','result':result})
            self.assertEqual(quantity_candidates(Path(temp))[valid_id]['quantity'],500)

    def test_queue_and_sheet_replay_stay_silent_for_uncaptioned_supply(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); folder = root / 'C123_1789771882.188909'; folder.mkdir()
            record, result = observed()
            record.update(caption='', status='awaiting_ai', thread_ts=record['message_ts'])
            result['fields']['items'][0]['receipt_assessment']['kind'] = 'existing_supply'
            manifest = folder / 'record.json'; write_json(manifest, record)
            reader = Mock(); reader.read.return_value = result
            client = Mock(); worker = LabelWorker(root,'C123',client,reader)
            self.assertEqual(worker.process(manifest)['reason'], 'no_clear_delivery_evidence')
            self.assertEqual(worker.process(manifest)['status'], 'skipped')
            reader.read.assert_called_once(); client.chat_postMessage.assert_not_called()
            store = FakeStore(); ReceiptSync(root,'C123',store).process(manifest)
            self.assertEqual(store.writes, 0)
            # Even a cached result still marked ready is guarded independently.
            write_json(folder/'analysis.json',{'status':'ready','result':result})
            ReceiptSync(root,'C123',store).process(manifest)
            self.assertEqual(store.writes,0)
