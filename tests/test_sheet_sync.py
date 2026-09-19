import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from label_reader import ITEM_FIELDS
from photo_intake import write_json
from sheet_sync import GoogleReceiptStore, HEADERS, ReceiptSync, SheetSyncError, receipt_rows


def sample():
    record = {'channel_id': 'C123', 'message_ts': '1789771882.188909', 'user_id': 'U123',
              'files': [{'file_id': 'F123', 'status': 'downloaded'}]}
    item = {**dict.fromkeys(ITEM_FIELDS), 'source_file_ids': ['F123'], 'label_type': 'product',
            'product': '=IMPORTXML("https://example.invalid", "x")', 'confidence': 'high',
            'receipt_assessment': {'kind': 'delivery', 'confidence': 'high', 'evidence': 'Sealed labeled case.'},
            'catalog_number': '00123', 'packaging_text': '10PK/CS (50EA/PK)',
            'expiry_printed': '20290130', 'expiry_iso': '2029-01-30'}
    result = {'fields': {'items': [item], 'caption_interpretation': {
        'received_quantity_statement': None, 'intended_storage': '365', 'confirmed_storage': None},
        'uncertainties': []}}
    return record, result


class FakeStore:
    def __init__(self):
        self.config = {'spreadsheet_id': 'test-sheet'}
        self.rows = {}
        self.writes = 0
        self.timeout_after_write = False

    def snapshot(self):
        return copy.deepcopy(self.rows)

    def read_row(self, row):
        return self.rows.get(row, [])

    def write_row(self, row, values):
        self.writes += 1
        self.rows[row] = values
        if self.timeout_after_write:
            raise TimeoutError()


class SyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.manifest = self.root / 'C123_1789771882.188909' / 'record.json'
        self.manifest.parent.mkdir()
        self.record, self.result = sample()
        write_json(self.manifest, self.record)
        write_json(self.manifest.with_name('analysis.json'), {'status': 'sent', 'result': self.result})
        self.store = FakeStore()
        self.now = 1000
        self.worker = ReceiptSync(self.root, 'C123', self.store, clock=lambda: self.now)

    def test_pack_size_and_intended_location_do_not_become_stock(self):
        row = receipt_rows(self.record, self.result)[0]
        self.assertEqual(row[7], '10PK/CS (50EA/PK)')
        self.assertEqual(row[8:12], ['', '', '365', ''])
        self.assertEqual(row[5], '00123')
        self.assertTrue(row[16].endswith('/p1789771882188909'))

    def test_cached_result_syncs_once_without_reader_or_slack_calls(self):
        self.assertEqual(self.worker.process(self.manifest)['status'], 'synced')
        ReceiptSync(self.root, 'C123', self.store).process(self.manifest)
        self.assertEqual(self.store.writes, 1)
        self.assertEqual(len(self.store.rows), 1)

    def test_timeout_after_commit_reconciles_by_id_even_when_sorted(self):
        self.store.timeout_after_write = True
        self.assertEqual(self.worker.process(self.manifest)['status'], 'retry_wait')
        row = self.store.rows.pop(5)
        row[3] = 'Operator corrected product'
        self.store.rows[8] = row
        self.now += 301
        self.assertEqual(self.worker.process(self.manifest)['status'], 'synced')
        self.assertEqual(self.store.writes, 1)
        self.assertEqual(self.store.rows[8][3], 'Operator corrected product')

    def test_reserved_row_conflict_is_not_overwritten(self):
        key = receipt_rows(self.record, self.result)[0][0]
        write_json(self.manifest.with_name('sheet-sync.json'), {'status': 'writing', 'reservations': {key: 5}})
        self.store.rows[5] = ['', '', '', 'Manually entered row']
        state = self.worker.process(self.manifest)
        self.assertEqual(state['error'], 'reserved_receipt_row_changed')
        self.assertEqual(self.store.writes, 0)

    def test_manual_row_with_empty_id_is_preserved(self):
        self.store.rows[5] = ['', '', '', 'Manual entry']
        self.worker.process(self.manifest)
        self.assertIn(6, self.store.rows)
        self.assertEqual(self.store.rows[5][3], 'Manual entry')

    def test_unrelated_images_and_not_ready_analysis_do_not_write(self):
        self.result['fields']['items'][0]['label_type'] = 'unrelated'
        write_json(self.manifest.with_name('analysis.json'), {'status': 'ready', 'result': self.result})
        self.assertEqual(self.worker.process(self.manifest)['status'], 'skipped')
        self.assertEqual(self.store.writes, 0)

    def test_multi_item_message_keeps_quantity_statement_message_scoped(self):
        self.result['fields']['items'].append(copy.deepcopy(self.result['fields']['items'][0]))
        self.result['fields']['caption_interpretation']['received_quantity_statement'] = 'received 2 boxes'
        rows = receipt_rows(self.record, self.result)
        self.assertNotEqual(rows[0][0], rows[1][0])
        self.assertEqual(rows[0][8:10], ['', ''])
        self.assertIn('Message receipt statement: received 2 boxes', rows[0][17])

    def test_failed_google_call_does_not_drop_receipt_and_retry_waits(self):
        self.store.snapshot = Mock(side_effect=SheetSyncError('google_authorization_required'))
        state = self.worker.process(self.manifest)
        self.assertEqual(state['status'], 'retry_wait')
        self.worker.process(self.manifest)
        self.store.snapshot.assert_called_once()

    def test_wrong_channel_rejected(self):
        self.record['channel_id'] = 'COTHER'
        write_json(self.manifest, self.record)
        with self.assertRaises(SheetSyncError):
            self.worker.process(self.manifest)

    def test_shortage_photo_is_not_imported_as_a_delivery(self):
        self.record['caption'] = 'Midiprep P2 runs out.'
        write_json(self.manifest, self.record)
        state = self.worker.process(self.manifest)
        self.assertEqual(state['reason'], 'no_package_labels')
        self.assertEqual(self.store.writes, 0)

    def test_refill_arrival_can_mention_a_shortage(self):
        self.record['caption'] = 'Received 2 boxes because we were running out.'
        write_json(self.manifest, self.record)
        self.assertEqual(self.worker.process(self.manifest)['status'], 'synced')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = GoogleReceiptStore({'account': 'linjhumse@gmail.com', 'spreadsheet_id': 'test-sheet',
                                       'tabs': {'Package receipts': {'sheet_id': 123}}})
        self.store.row_count = 104

    def test_atomic_batch_uses_literal_strings_and_only_target_row(self):
        self.store.request = Mock(return_value={})
        values = receipt_rows(*sample())[0]
        self.store.write_row(5, values)
        body = self.store.request.call_args.kwargs['json']
        update = body['requests'][0]['updateCells']
        self.assertEqual(update['start'], {'sheetId': 123, 'rowIndex': 4, 'columnIndex': 0})
        cells = update['rows'][0]['values']
        self.assertEqual(cells[3]['userEnteredValue'], {'stringValue': values[3]})
        self.assertEqual(cells[5]['userEnteredValue'], {'stringValue': '00123'})
        self.assertNotIn('formulaValue', json.dumps(body))

    def test_existing_formula_or_validation_blocks_write(self):
        for cell in ({'userEnteredValue': {'formulaValue': '=""'}}, {'dataValidation': {'strict': True}}):
            self.store.request = Mock(return_value={'sheets': [{'data': [{'rowData': [{'values': [cell]}]}]}]})
            with self.assertRaisesRegex(SheetSyncError, 'reserved_receipt_row_changed'):
                self.store.write_row(5, receipt_rows(*sample())[0])
            self.assertEqual(self.store.request.call_count, 1)

    def test_header_change_blocks_write(self):
        self.store.request = Mock(return_value={'sheets': [{'properties': {'sheetId': 123,
            'title': 'Package receipts', 'gridProperties': {'rowCount': 104, 'columnCount': 18}}}]})
        self.store.values = Mock(return_value=[HEADERS[::-1]])
        with self.assertRaisesRegex(SheetSyncError, 'receipt_headers_changed'):
            self.store.snapshot()


if __name__ == '__main__':
    unittest.main()
