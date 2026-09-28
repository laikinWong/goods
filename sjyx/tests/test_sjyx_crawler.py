import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from sjyx_crawler import (
    ApiClient,
    CrawlerConfig,
    SjyxCrawler,
    PRICE_FIELD_NAMES,
    JsonlWriter,
    flatten_categories,
    normalize_product,
    remove_price_fields,
    should_log_detail_progress,
)


class TlsConfigurationTests(unittest.TestCase):
    def test_api_client_uses_certifi_ca_bundle(self):
        config = CrawlerConfig(output_dir=Path("unused"))
        with patch("sjyx_crawler.certifi.where", return_value="/tmp/test-ca.pem"), patch(
            "sjyx_crawler.ssl.create_default_context"
        ) as create_context:
            client = ApiClient(config)

        create_context.assert_called_once_with(cafile="/tmp/test-ca.pem")
        self.assertIs(client.ssl_context, create_context.return_value)


class CategoryTests(unittest.TestCase):
    def test_flatten_categories_keeps_full_path_for_leaf_nodes(self):
        tree = [
            {
                "id": "root-1",
                "title": "土建材料",
                "cascadeId": "root-1",
                "children": [
                    {
                        "id": "child-1",
                        "title": "钢筋类",
                        "cascadeId": "root-1,child-1",
                        "children": [],
                    }
                ],
            }
        ]

        leaves = flatten_categories(tree)

        self.assertEqual(
            leaves,
            [
                {
                    "ids": ["root-1", "child-1"],
                    "names": ["土建材料", "钢筋类"],
                    "cascade_id": "root-1,child-1",
                    "leaf_id": "child-1",
                }
            ],
        )


class SanitizationTests(unittest.TestCase):
    def test_remove_price_fields_recursively_ignores_case_variants(self):
        raw = {
            "name": "商品",
            "bestSkuSalesPrice": 12.3,
            "detail": {
                "salesPrice": 45,
                "nested": [{"lowestPrice": 1, "model": "M1"}],
            },
            "attributeList": [{"attributeName": "规格", "attributeValue": "A"}],
        }

        cleaned = remove_price_fields(raw)

        self.assertEqual(cleaned["name"], "商品")
        self.assertEqual(cleaned["detail"]["nested"][0], {"model": "M1"})
        self.assertEqual(cleaned["attributeList"][0]["attributeValue"], "A")
        lower_keys = {key.lower() for key in PRICE_FIELD_NAMES}
        self.assertFalse(any(key.lower() in lower_keys for key in json.dumps(cleaned)))


class ProductNormalizationTests(unittest.TestCase):
    def test_normalize_product_combines_category_list_and_detail_without_prices(self):
        category = {
            "ids": ["c1", "c2"],
            "names": ["一级", "二级"],
            "cascade_id": "c1,c2",
            "leaf_id": "c2",
        }
        listing = {
            "sysNo": "spu-1",
            "bestSkuSysNo": "sku-1",
            "name": "测试商品",
            "brand": "测试品牌",
            "shopName": "测试店铺",
            "shopSysNo": "shop-1",
            "unit": "台",
            "images": ["/image.jpg"],
            "bestSkuSalesPrice": 999,
            "attributeList": [
                {"attributeName": "规格", "attributeValue": "100x200"},
                {"attributeName": "颜色", "attributeValue": "蓝色"},
            ],
        }
        detail = {
            "sysNo": "sku-1",
            "spuSysNo": "spu-1",
            "model": "MX-1",
            "brandName": "详情品牌",
            "salesPrice": 1000,
            "recommendSpuDTO": {
                "sysNo": "other-spu",
                "name": "不属于当前商品的推荐商品",
            },
            "attributeList": [
                {
                    "attributeName": "规格型号",
                    "categoryAttributeValueList": [
                        {"attributeValue": "MX-1", "salesPrice": 1}
                    ],
                }
            ],
        }

        normalized = normalize_product(category, listing, detail)

        self.assertEqual(normalized["category"]["names"], ["一级", "二级"])
        self.assertEqual(normalized["product"]["spu_sys_no"], "spu-1")
        self.assertEqual(normalized["product"]["sku_sys_no"], "sku-1")
        self.assertEqual(normalized["product"]["spec_model"], "MX-1")
        self.assertEqual(normalized["product"]["attributes"][0]["attributeValue"], "100x200")
        serialized = json.dumps(normalized, ensure_ascii=False)
        self.assertNotIn("bestSkuSalesPrice", serialized)
        self.assertNotIn("salesPrice", serialized)
        self.assertNotIn("recommendSpuDTO", serialized)
        self.assertNotIn("不属于当前商品的推荐商品", serialized)


class JsonlWriterTests(unittest.TestCase):
    def test_jsonl_writer_appends_one_json_object_per_line(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "products.jsonl"
            writer = JsonlWriter(path)

            writer.append({"sku": "1"})
            writer.append({"sku": "2"})

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual([json.loads(line)["sku"] for line in lines], ["1", "2"])


class ProgressTests(unittest.TestCase):
    def test_should_log_first_interval_and_last_detail_item(self):
        self.assertTrue(should_log_detail_progress(1, 200, 20))
        self.assertFalse(should_log_detail_progress(19, 200, 20))
        self.assertTrue(should_log_detail_progress(20, 200, 20))
        self.assertTrue(should_log_detail_progress(200, 200, 20))


class ConcurrencyTests(unittest.TestCase):
    def test_detail_requests_use_configured_concurrency(self):
        barrier = threading.Barrier(2)
        with tempfile.TemporaryDirectory() as temp_dir:
            config = CrawlerConfig(
                output_dir=Path(temp_dir),
                request_interval_ms=0,
                concurrency=2,
            )
            crawler = SjyxCrawler(config)
            crawler.client.post = lambda *args, **kwargs: {
                "data": {
                    "spuList": {
                        "total": 2,
                        "totalPage": 1,
                        "datas": [
                            {"bestSkuSysNo": "sku-1", "name": "one"},
                            {"bestSkuSysNo": "sku-2", "name": "two"},
                        ],
                    }
                }
            }

            def fetch_detail(sku):
                barrier.wait(timeout=2)
                return {"sysNo": sku}

            crawler.fetch_detail = fetch_detail
            crawler.crawl_category(
                {"cascade_id": "c1", "ids": ["c1"], "names": ["分类"], "leaf_id": "c1"}
            )

            self.assertEqual(crawler.catalog.source_count("sjyx", "success"), 2)
            crawler.catalog.finish_run(crawler.run_id)
            crawler.catalog.close()


if __name__ == "__main__":
    unittest.main()
