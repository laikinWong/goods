import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from catalog_store import CatalogStore
from migrate_catalog import BdtJSONRepairReader, import_source


class CatalogStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = CatalogStore(self.root / "catalog.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_successful_ids_are_batched_and_scoped_by_source(self):
        run_id = self.store.start_run("sjyx")
        self.store.register_discovered("sjyx", ((str(i), "cat") for i in range(700)), run_id)
        self.store.mark_success("sjyx", "3", {"id": 3}, run_id, "cat")
        self.store.mark_success("other", "4", {"id": 4}, run_id, "cat")

        found = self.store.ids_with_statuses("sjyx", (str(i) for i in range(700)))

        self.assertEqual(found, {"3"})

    def test_failed_product_remains_retryable_then_becomes_success(self):
        run_id = self.store.start_run("bdt")
        self.store.register_discovered("bdt", [(10, "c")], run_id)
        self.store.mark_failed("bdt", 10, "timeout", run_id)
        self.assertEqual(self.store.ids_with_statuses("bdt", [10]), set())
        self.assertEqual(self.store.ids_with_statuses("bdt", [10], ("failed",)), {"10"})

        self.store.mark_success("bdt", 10, {"id": 10, "name": "ok"}, run_id, "c")

        self.assertEqual(self.store.ids_with_statuses("bdt", [10]), {"10"})
        self.assertEqual(list(self.store.iter_payloads("bdt"))[0]["name"], "ok")

    def test_run_stats_track_scan_skip_new_and_failure(self):
        run_id = self.store.start_run("lcgt")
        self.store.register_discovered("lcgt", [("1", "c"), ("2", "c")], run_id)
        self.store.increment_run(run_id, skipped_count=1)
        self.store.mark_success("lcgt", "1", {"id": "1"}, run_id)
        self.store.mark_failed("lcgt", "2", "bad", run_id)
        self.store.finish_run(run_id)

        stats = self.store.run_stats(run_id)

        self.assertEqual(stats["status"], "success")
        self.assertEqual(stats["scanned_count"], 2)
        self.assertEqual(stats["skipped_count"], 1)
        self.assertEqual(stats["new_count"], 1)
        self.assertEqual(stats["failed_count"], 1)


class CatalogMigrationTests(unittest.TestCase):
    @staticmethod
    def fake_ijson():
        def items(handle, prefix, use_float=True):
            del prefix, use_float
            yield from json.load(handle)

        def kvitems(handle, prefix, use_float=True):
            del prefix, use_float
            yield from json.load(handle).items()

        return types.SimpleNamespace(items=items, kvitems=kvitems)

    def test_sjyx_jsonl_import_is_idempotent_and_reports_invalid_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "products.jsonl"
            records = [
                {"category": {"cascade_id": "c1"}, "product": {"sku_sys_no": "s1"}},
                {"category": {"cascade_id": "c1"}, "product": {"sku_sys_no": "s1"}},
                {"category": {"cascade_id": "c2"}, "product": {"sku_sys_no": ""}},
            ]
            source.write_text(
                "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
                encoding="utf-8",
            )

            with CatalogStore(root / "catalog.sqlite3") as store:
                first = import_source(store, "sjyx", source)
                second = import_source(store, "sjyx", source)

                self.assertEqual(first.records, 3)
                self.assertEqual(first.valid, 2)
                self.assertEqual(first.invalid, 1)
                self.assertEqual(first.unique_added, 1)
                self.assertEqual(second.unique_added, 0)
                self.assertEqual(store.source_count("sjyx"), 1)

    def test_array_and_nested_importers_extract_bdt_and_lcgt_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bdt_path = root / "bdt.json"
            lcgt_path = root / "lcgt.json"
            bdt_path.write_text(json.dumps([{"id": 7}, {"id": 8}]), encoding="utf-8")
            lcgt_path.write_text(
                json.dumps(
                    {
                        "钢材": {
                            "钢板": {
                                "subcategory_id": "sub-1",
                                "products": [{"id": "p1"}, {"id": "p2"}],
                            }
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch.dict("sys.modules", {"ijson": self.fake_ijson()}):
                with CatalogStore(root / "catalog.sqlite3") as store:
                    bdt = import_source(store, "bdt", bdt_path)
                    lcgt = import_source(store, "lcgt", lcgt_path)

                    self.assertEqual(bdt.unique_added, 2)
                    self.assertEqual(lcgt.unique_added, 2)
                    self.assertEqual(store.ids_with_statuses("bdt", [7, 8]), {"7", "8"})
                    self.assertEqual(store.ids_with_statuses("lcgt", ["p1", "p2"]), {"p1", "p2"})

    def test_bdt_reader_repairs_only_truncated_content_value(self):
        import io

        raw = (
            b'[\n  {\n    "id": 1,\n'
            b'    "content": "broken base64/\n'
            b'    "poster": "ok"\n  }\n]'
        )
        reader = BdtJSONRepairReader(io.BytesIO(raw))

        repaired = json.loads(reader.read())

        self.assertEqual(repaired, [{"id": 1, "content": "", "poster": "ok"}])
        self.assertEqual(reader.repaired_count, 1)


if __name__ == "__main__":
    unittest.main()
