"""Check real crawler finalizers using local, deterministic request substitutes."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import crawler_control as control
from bdt import scraper as bdt
from lcgt import lcgt_crawler as lcgt
from sjyx import sjyx_crawler as sjyx


class CrawlerFinishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.install = patch.object(control, 'install')
        self.install.start()
        control._finishing = False

    def tearDown(self):
        control._finishing = False
        self.install.stop()
        self.tmp.cleanup()

    def test_bdt_flushes_details_before_category_completes(self):
        with patch.object(bdt, 'OUTPUT_DIR', str(self.root)), \
             patch.object(bdt, 'step1_get_categories', return_value=[{'id':1, 'name':'a'}]), \
             patch.object(bdt, 'step2_get_subcategories', return_value=[{'sons':[{'id':2, 'name':'b'}]}]), \
             patch.object(bdt, 'step3_get_goods_list', return_value=[{'id':3},{'id':4}]), \
             patch.object(bdt, 'step4_get_goods_detail', side_effect=[{'id':3},control.FinishRequested()]), \
             patch.object(bdt.time, 'sleep'):
            bdt.main()
        for name in ['goods_details_partial.json', 'goods_details_all.json']:
            self.assertEqual(json.loads((self.root / name).read_text()), [{'id':3}])

    def test_bdt_uses_user_selected_detail_concurrency(self):
        barrier = threading.Barrier(2)

        def detail(goods_id):
            barrier.wait(timeout=2)
            return {'id': goods_id}

        performance = {'GOODS_REQUEST_INTERVAL_MS': '0', 'GOODS_CONCURRENCY': '2'}
        with patch.dict(os.environ, performance), \
             patch.object(bdt, 'OUTPUT_DIR', str(self.root)), \
             patch.object(bdt, 'step1_get_categories', return_value=[{'id':1, 'name':'a'}]), \
             patch.object(bdt, 'step2_get_subcategories', return_value=[{'sons':[{'id':2, 'name':'b'}]}]), \
             patch.object(bdt, 'step3_get_goods_list', return_value=[{'id':3},{'id':4}]), \
             patch.object(bdt, 'step4_get_goods_detail', side_effect=detail):
            bdt.main()

        records = json.loads((self.root / 'goods_details_all.json').read_text())
        self.assertEqual({item['id'] for item in records}, {3, 4})

    def test_lcgt_preserves_incomplete_category_and_previous_records(self):
        output = self.root / 'lcgt_products.json'
        output.write_text(json.dumps({'钢': {'板': {'count':1, 'complete':False, 'products':[{'id':1}]}}}))
        def crawl(*args):
            args[-1].append({'id':2, 'main_image':'saved.jpg'})
            control._finishing = True
            raise control.FinishRequested()
        categories = {'1': {'name':'钢','subcategories':{'2':{'name':'板','spec_key':'x'}}}}
        with patch.object(lcgt, '__file__', str(self.root / 'crawler.py')), \
             patch.object(lcgt, 'get_categories', return_value=categories), \
             patch.object(lcgt, 'crawl_subcategory', side_effect=crawl):
            with self.assertRaises(control.FinishRequested):
                lcgt.main()
        saved = json.loads(output.read_text())['钢']['板']
        self.assertFalse(saved['complete'])
        self.assertEqual(saved['count'], 2)
        self.assertEqual(saved['products'][1]['main_image'], 'saved.jpg')

    def test_sjyx_exports_json_on_finish(self):
        def crawl(instance):
            instance.products.append({'product': {'sku_sys_no':'123'}})
            raise control.FinishRequested()
        with patch.object(sjyx.SjyxCrawler, 'run', crawl):
            self.assertEqual(sjyx.main(['--output-dir', str(self.root), '--export-json']), 0)
        self.assertEqual(json.loads((self.root / 'products.json').read_text()), [{'product': {'sku_sys_no':'123'}}])
