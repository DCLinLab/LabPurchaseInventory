import copy
import json
from pathlib import Path
import tempfile
import unittest

from photo_intake import write_json
from receipt_quantity import infer_quantity, quantity_candidates, receipt_id, sync_quantities
from test_sheet_sync import sample


def observed(count=1, unit='case'):
    record, result = sample()
    record['files'][0]['sha256'] = 'same-photo-hash'
    item = result['fields']['items'][0]
    item['brand_or_supplier'] = 'basix / Fisher Scientific'
    item['package_observation'] = {'distinct_packages': count, 'package_unit': unit,
                                 'evidence': 'Label is attached to one outer case.'}
    return record, result


class QuantityTests(unittest.TestCase):
    def test_one_case_ten_packs_fifty_each_is_500(self):
        _, result = observed()
        q = infer_quantity(result['fields']['items'][0])
        self.assertEqual((q['quantity'], q['unit']), (500, 'each'))

    def test_inner_pack_not_outer_case_and_multiple_cases(self):
        _, result = observed(unit='pack')
        self.assertEqual(infer_quantity(result['fields']['items'][0])['quantity'], 50)
        _, result = observed(3)
        self.assertEqual(infer_quantity(result['fields']['items'][0])['quantity'], 1500)

    def test_ambiguous_carton_low_quality_and_no_observation_stay_unknown(self):
        for change in ({'package_observation': None}, {'confidence': 'low'},
                       {'package_observation': {'distinct_packages': None, 'package_unit': 'case', 'evidence': 'ambiguous'}},
                       {'package_observation': {'distinct_packages': 1, 'package_unit': 'carton', 'evidence': 'shipping carton'}}):
            _, result = observed()
            result['fields']['items'][0].update(change)
            self.assertIsNone(infer_quantity(result['fields']['items'][0]))

    def test_email_pack_size_can_supply_contents_but_conflict_blocks_inference(self):
        _, result = observed()
        item = result['fields']['items'][0]
        order = {'catalog': '00-123', 'supplier': 'Fisher Scientific', 'unit': 'case', 'pack_size': 'Case of 500'}
        self.assertEqual(infer_quantity(item, [order])['quantity'], 500)
        order['pack_size'] = 'Case of 100'
        self.assertIsNone(infer_quantity(item, [order]))
        item['packaging_text'] = None
        self.assertEqual(infer_quantity(item, [order])['quantity'], 100)
        self.assertIsNone(infer_quantity(item, [order, dict(order, pack_size='Case of 50')]))

    def test_repeat_images_shortages_and_mixed_items_do_not_add_quantities(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index in range(2):
                record, result = observed()
                record['message_ts'] = f'100.{index}'
                folder = root / f'C123_100.{index}'
                folder.mkdir()
                write_json(folder / 'record.json', record)
                write_json(folder / 'analysis.json', {'status': 'sent', 'result': result})
            self.assertEqual(len(quantity_candidates(root)), 1)
            for p in root.glob('*/record.json'):
                record = json.loads(p.read_text());record['caption'] = 'Running out of these.';write_json(p, record)
            self.assertEqual(quantity_candidates(root), {})

    def test_two_views_of_one_case_are_not_multiplied(self):
        record, result = observed()
        record['files'].append({'file_id': 'F456', 'status': 'downloaded', 'sha256': 'second-view'})
        result['fields']['items'][0]['source_file_ids'].append('F456')
        self.assertEqual(infer_quantity(result['fields']['items'][0])['quantity'], 500)


class SyncTests(unittest.TestCase):
    def test_atomic_write_is_verified_and_not_repeated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / 'C123_100.1';folder.mkdir()
            record, result = observed()
            write_json(folder / 'record.json', record)
            write_json(folder / 'analysis.json', {'status': 'sent', 'result': result})
            class Store:
                sheet_id = 12
                base = 'test'
                def __init__(self):
                    self.row = [receipt_id(record, 0)] + [''] * 17
                    self.writes = 0
                def snapshot(self): return {5: list(self.row)}
                def read_row(self, row): return list(self.row)
                def request(self, method, url, **kwargs):
                    if method == 'GET': return {}
                    self.writes += 1
                    for request in kwargs['json']['requests']:
                        if 'updateCells' in request:
                            update = request['updateCells'];start=update['range']['startColumnIndex']
                            for i, cell in enumerate(update['rows'][0]['values']):
                                self.row[start+i] = next(iter(cell['userEnteredValue'].values()))
                    return {}
            store = Store()
            self.assertEqual(sync_quantities(root, store), 1)
            self.assertEqual(store.row[8:10], [500, 'each'])
            self.assertEqual(sync_quantities(root, store), 0)
            store.row[8] = 450  # User correction remains intact.
            self.assertEqual(sync_quantities(root, store), 0)
            self.assertEqual(store.writes, 1)


if __name__ == '__main__':
    unittest.main()
