import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import run_crawlers


class RunCrawlersTests(unittest.TestCase):
    def test_expand_all_targets_preserves_registry_order(self):
        self.assertEqual(
            run_crawlers.expand_targets(["all"]),
            ["sjyx", "lcgt", "bdt"],
        )

    def test_expand_multiple_targets_deduplicates_in_input_order(self):
        self.assertEqual(
            run_crawlers.expand_targets(["lcgt", "sjyx", "lcgt"]),
            ["lcgt", "sjyx"],
        )

    def test_expand_unknown_target_raises_clear_error(self):
        with self.assertRaisesRegex(ValueError, "Unknown crawler target: missing"):
            run_crawlers.expand_targets(["missing"])

    def test_registered_crawler_script_paths_exist(self):
        for spec in run_crawlers.CRAWLERS.values():
            with self.subTest(spec.name):
                self.assertTrue(Path(spec.command[1]).exists(), spec.command[1])

    def test_dry_run_creates_log_dir_and_returns_success_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                result = run_crawlers.run(
                    targets=["sjyx", "bdt"],
                    parallel=True,
                    dry_run=True,
                    quiet=True,
                    log_dir=Path(tmp),
                )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual([item.name for item in result.items], ["sjyx", "bdt"])
            self.assertTrue(result.run_log_dir.exists())
            self.assertTrue((result.run_log_dir / "orchestrator.log").exists())

    def test_bdt_uses_bdt_output_filename_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                result = run_crawlers.run(
                    targets=["bdt"],
                    dry_run=True,
                    quiet=True,
                    log_dir=tmp_path / "logs",
                    data_dir=tmp_path / "data",
                    run_timestamp="20260702090721",
                )

            self.assertEqual(
                result.items[0].final_output_path,
                tmp_path / "data" / "bdt-20260702090721.json",
            )

    def test_badatong_target_is_no_longer_valid(self):
        with self.assertRaisesRegex(ValueError, "Unknown crawler target: badatong"):
            run_crawlers.expand_targets(["badatong"])

    def test_main_returns_nonzero_for_invalid_target(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code = run_crawlers.main(["--target", "missing", "--dry-run", "--quiet"])
        self.assertEqual(code, 2)

    def test_run_returns_nonzero_when_child_command_fails(self):
        original = run_crawlers.CRAWLERS["sjyx"]
        run_crawlers.CRAWLERS["sjyx"] = run_crawlers.CrawlerSpec(
            "sjyx",
            [run_crawlers.PYTHON, "-c", "import sys; sys.exit(7)"],
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with contextlib.redirect_stdout(io.StringIO()):
                    result = run_crawlers.run(
                        targets=["sjyx"],
                        parallel=False,
                        dry_run=False,
                        quiet=True,
                        log_dir=Path(tmp),
                    )

                self.assertTrue(result.items[0].log_path.exists())
        finally:
            run_crawlers.CRAWLERS["sjyx"] = original

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.items[0].exit_code, 7)

    def test_successful_run_copies_primary_output_to_data_dir_with_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_path = tmp_path / "source.json"
            data_dir = tmp_path / "data"
            original = run_crawlers.CRAWLERS["sjyx"]
            run_crawlers.CRAWLERS["sjyx"] = run_crawlers.CrawlerSpec(
                "sjyx",
                [
                    run_crawlers.PYTHON,
                    "-c",
                    (
                        "from pathlib import Path; "
                        f"Path({str(source_path)!r}).write_text('[1]', encoding='utf-8')"
                    ),
                ],
                source_path,
            )
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    result = run_crawlers.run(
                        targets=["sjyx"],
                        parallel=False,
                        dry_run=False,
                        quiet=True,
                        log_dir=tmp_path / "logs",
                        data_dir=data_dir,
                        run_timestamp="20260702090721",
                    )
            finally:
                run_crawlers.CRAWLERS["sjyx"] = original

            final_path = data_dir / "sjyx-20260702090721.json"
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.items[0].final_output_path, final_path)
            self.assertEqual(final_path.read_text(encoding="utf-8"), "[1]")


if __name__ == "__main__":
    unittest.main()
