import copy
import tempfile
import unittest
from pathlib import Path

from inventory_sync import build_rows, each_quantity, sync_rows
from receipt_quantity import receipt_id
from sheet_sync import SheetSyncError, receipt_rows
from test_receipt_quantity import observed


def sources(count=1):
    record, result = observed(count)
    r = receipt_rows(record, result)[0]
    r[8:10] = [500 * count, 'each']
    r[17] += '; Inferred received: packages'
    o = ['A123', 46000, 'Person', 'Fisher Scientific', 'Tube', '00123',
         '15 mL; Case of 500', 3, 'case', 'Shipped 3 of 3', '', 'email-source', '']
    return o, r


class TotalsTests(unittest.TestCase):
    def test_provisional_and_confirmed_order_matches(self):
        o, r = sources()
        row = next(iter(build_rows([o], [r]).values()))
        self.assertEqual(row[4:9], [1500, 500, 1000, 'each', 'Provisional order match'])
        self.assertEqual(row[9], '')  # Intended storage never becomes confirmed.
        r[15] = 'A123'
        self.assertEqual(next(iter(build_rows([o], [r]).values()))[8], 'Matched')

    def test_three_distinct_receipts_complete_order_and_repeated_image_does_not(self):
        o, r = sources()
        rs = []
        for i in range(3):
            new = list(r); new[0] = f'R-{i}'; new[16] = f'slack-{i}'; new[15] = 'A123'
            rs.append(new)
        dup = list(rs[0]); dup[0] = 'R-duplicate'; dup[16] = 'slack-duplicate'; rs.append(dup)
        hashes = {'R-0': {'photo0'}, 'R-duplicate': {'photo0'}}
        row = next(iter(build_rows([o], rs, hashes).values()))
        self.assertEqual(row[4:9], [1500, 1500, 0, 'each', 'Matched'])
        self.assertIn('Excluded 1', row[11])
        self.assertEqual(build_rows([o], rs, hashes), build_rows([o], rs, hashes))

    def test_unknown_quantity_and_unit_are_not_zero_or_guessed(self):
        o, r = sources(); r[8:10] = ['', '']
        row = next(iter(build_rows([o], [r]).values()))
        self.assertEqual(row[6], '')
        self.assertEqual(row[8], 'Receipt quantity incomplete')
        self.assertIsNone(each_quantity(3, 'carton', 'Case of 500'))
        self.assertIsNone(each_quantity(True, 'each', ''))
        o[8] = 'carton'
        self.assertEqual(next(iter(build_rows([o], [r]).values()))[4], '')

    def test_multiple_orders_not_implicitly_allocated_and_overreceipt_flagged(self):
        o, r = sources(); other = list(o); other[0] = 'A456'
        row = next(iter(build_rows([o, other], [r]).values()))
        self.assertEqual(row[4:7], [3000, 500, 2500])
        self.assertEqual(row[8], 'Provisional order match')
        r[8] = 2000; r[15] = 'A123'
        self.assertEqual(next(iter(build_rows([o, other], [r]).values()))[8], 'Order allocation needs review')

    def test_duplicate_ids_fail_and_different_suppliers_do_not_merge(self):
        o, r = sources()
        with self.assertRaises(SheetSyncError): build_rows([o, o], [r])
        with self.assertRaises(SheetSyncError): build_rows([o], [r, r])
        r[4] = 'Other supplier'
        self.assertEqual(len(build_rows([o], [r])), 2)


class Store:
    config = {'spreadsheet_id': 'test'}
    def __init__(self): self.rows = {}; self.writes = 0; self.lose_response = False
    def snapshot(self): return copy.deepcopy(self.rows)
    def read_row(self, row): return self.rows.get(row, [''] * 12)
    def write_row(self, row, values, expected):
        assert self.read_row(row) == expected
        self.rows[row] = list(values); self.writes += 1
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError()


class SyncTests(unittest.TestCase):
    def test_retries_sorting_manual_edits_and_source_corrections(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'journal.json'; store = Store(); o, r = sources()
            desired = build_rows([o], [r]); key = next(iter(desired))
            store.lose_response = True
            with self.assertRaises(TimeoutError): sync_rows(desired, store, path)
            store.rows[9] = store.rows.pop(5)  # Lost response followed by a sheet sort.
            self.assertEqual(sync_rows(desired, store, path)[key], 'synced')
            self.assertEqual(store.writes, 1)
            r[8] = 450
            sync_rows(build_rows([o], [r]), store, path)
            self.assertEqual(store.rows[9][4:7], [1500, 450, 1050])
            store.rows[9][5] = 400
            self.assertEqual(sync_rows(build_rows([o], [r]), store, path)[key], 'needs_review')
            self.assertEqual(store.rows[9][5], 400)

    def test_deleted_sources_do_not_leave_stale_totals(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'journal.json'; store = Store(); o, r = sources()
            sync_rows(build_rows([o], [r]), store, path)
            sync_rows({}, store, path)
            self.assertEqual(store.rows[5][4:7], ['', 0, ''])
            self.assertEqual(store.rows[5][8], 'Source records removed')


if __name__ == '__main__': unittest.main()
