import os
import unittest
from unittest.mock import patch

from crawler_performance import (
    RequestRateLimiter,
    settings_from_environment,
    validate_performance_settings,
)


class PerformanceSettingsTests(unittest.TestCase):
    def test_defaults_and_per_platform_values(self):
        values = validate_performance_settings(
            ["sjyx", "bdt"],
            {"sjyx": {"request_interval_ms": 1250, "concurrency": 5}},
        )
        self.assertEqual(values["sjyx"], {"request_interval_ms": 1250, "concurrency": 5})
        self.assertEqual(values["bdt"], {"request_interval_ms": 300, "concurrency": 1})

    def test_environment_values_are_immutable_settings(self):
        with patch.dict(
            os.environ,
            {"GOODS_REQUEST_INTERVAL_MS": "450", "GOODS_CONCURRENCY": "6"},
        ):
            settings = settings_from_environment("lcgt")
        self.assertEqual(settings.request_interval_ms, 450)
        self.assertEqual(settings.concurrency, 6)
        with self.assertRaises(Exception):
            settings.concurrency = 2


class RequestRateLimiterTests(unittest.TestCase):
    def test_request_starts_are_spaced_in_milliseconds(self):
        limiter = RequestRateLimiter(100)
        with patch("crawler_performance.time.monotonic", side_effect=[10.0, 10.02, 10.1]), patch(
            "crawler_performance.time.sleep"
        ) as sleep:
            limiter.wait()
            limiter.wait()
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.08, places=6)


if __name__ == "__main__":
    unittest.main()
