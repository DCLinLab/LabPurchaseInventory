import copy
import tempfile
import unittest
from pathlib import Path

from label_reader import render_reply
from photo_intake import write_json
from receipt_quantity import infer_quantity, quantity_candidates, receipt_id
from test_receipt_quantity import observed


def declared(item, count=4, unit='case', scope='this_item', quote='received 4 cases'):
    item['stated_quantity'] = dict(count=count, unit=unit, scope=scope, quote=quote)


class StatedQuantityTests(unittest.TestCase):
    def test_member_total_replaces_visible_count_and_is_traced_in_reply(self):
        record, result = observed(); item=result['fields']['items'][0]
        record['caption']='received 4 cases'; declared(item)
        q=infer_quantity(item,caption=record['caption'])
        self.assertEqual(q['quantity'],2000)
        self.assertIn('member-stated total: received 4 cases',q['note'])
        self.assertIn('2000',render_reply(result,record))
        self.assertEqual(item['package_observation']['distinct_packages'],1)

    def test_packs_each_and_identical_packages_use_correct_units(self):
        _, result=observed(); item=result['fields']['items'][0]
        for unit,total in [('pack',200),('each',4),('package',2000)]:
            declared(item,unit=unit)
            self.assertEqual(infer_quantity(item)['quantity'],total)
        item['package_observation']=None
        declared(item)
        self.assertEqual(infer_quantity(item)['quantity'],2000)

    def test_cartons_and_unknown_package_contents_preserve_reported_count(self):
        _, result=observed(); item=result['fields']['items'][0]
        declared(item,unit='carton')
        q=infer_quantity(item)
        self.assertEqual((q['quantity'],q['unit'],q['per_package']),(4,'carton',None))
        item['package_observation']=None; declared(item,unit='package')
        self.assertEqual(infer_quantity(item)['unit'],'package')

    def test_ambiguous_invalid_or_unquoted_claim_never_falls_back_to_one(self):
        _, result=observed(); item=result['fields']['items'][0]
        for change in ({'scope':'ambiguous'},{'count':None},{'count':0},{'count':True},{'quote':None},{'unit':'unknown'}):
            declared(item); item['stated_quantity'].update(change)
            self.assertIsNone(infer_quantity(item))
        declared(item)
        self.assertIsNone(infer_quantity(item,caption='No such receipt statement'))
        item['receipt_assessment']['kind']='existing_supply'
        self.assertIsNone(infer_quantity(item))

    def test_allocated_multiple_products_and_reprocessing_do_not_multiply_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); folder=root/'message';folder.mkdir()
            record,result=observed(); item=result['fields']['items'][0]
            record['caption']='received 4 cases of A and 2 packs of B'
            declared(item,quote='received 4 cases of A')
            other=copy.deepcopy(item);other['catalog_number']='B123'
            declared(other,count=2,unit='pack',quote='2 packs of B')
            result['fields']['items'].append(other)
            write_json(folder/'record.json',record)
            write_json(folder/'analysis.json',{'status':'ready','result':result})
            rows=quantity_candidates(root)
            self.assertEqual(rows[receipt_id(record,0)]['quantity'],2000)
            self.assertEqual(rows[receipt_id(record,1)]['quantity'],100)
            self.assertEqual(quantity_candidates(root),rows)
            for i in result['fields']['items']: i['stated_quantity']['scope']='ambiguous'
            write_json(folder/'analysis.json',{'status':'ready','result':result})
            self.assertEqual(quantity_candidates(root),{})
