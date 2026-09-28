"""Shared, immutable per-run crawler performance settings."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, Iterable, Mapping, Optional


MIN_INTERVAL_MS = 0
MAX_INTERVAL_MS = 600_000
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 32

PERFORMANCE_DEFAULTS: Dict[str, Dict[str, int]] = {
    "sjyx": {"request_interval_ms": 2_000, "concurrency": 1},
    "lcgt": {"request_interval_ms": 1_000, "concurrency": 3},
    "bdt": {"request_interval_ms": 300, "concurrency": 1},
}


@dataclass(frozen=True)
class PerformanceSettings:
    request_interval_ms: int
    concurrency: int

    def as_dict(self) -> Dict[str, int]:
        return asdict(self)


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def validate_performance_settings(
    targets: Iterable[str],
    raw_settings: Any = None,
) -> Dict[str, Dict[str, int]]:
    """Validate API input and fill omitted targets with safe defaults."""
    target_list = list(targets)
    if raw_settings is None:
        raw_settings = {}
    if not isinstance(raw_settings, Mapping):
        raise ValueError("性能设置必须是对象")
    unexpected = set(raw_settings) - set(target_list)
    if unexpected:
        raise ValueError("性能设置包含未启动的平台：" + "、".join(sorted(unexpected)))

    normalized: Dict[str, Dict[str, int]] = {}
    for target in target_list:
        defaults = PERFORMANCE_DEFAULTS[target]
        value = raw_settings.get(target, defaults)
        if not isinstance(value, Mapping):
            raise ValueError(f"{target} 的性能设置必须是对象")
        interval = _integer(
            value.get("request_interval_ms", defaults["request_interval_ms"]),
            f"{target} 请求间隔",
            MIN_INTERVAL_MS,
            MAX_INTERVAL_MS,
        )
        concurrency = _integer(
            value.get("concurrency", defaults["concurrency"]),
            f"{target} 并发数量",
            MIN_CONCURRENCY,
            MAX_CONCURRENCY,
        )
        normalized[target] = {
            "request_interval_ms": interval,
            "concurrency": concurrency,
        }
    return normalized


def settings_from_environment(source: str) -> PerformanceSettings:
    defaults = PERFORMANCE_DEFAULTS[source]
    try:
        interval = int(os.environ.get("GOODS_REQUEST_INTERVAL_MS", defaults["request_interval_ms"]))
        concurrency = int(os.environ.get("GOODS_CONCURRENCY", defaults["concurrency"]))
    except ValueError as exc:
        raise ValueError("请求间隔和并发数量必须是整数") from exc
    values = validate_performance_settings(
        [source],
        {source: {"request_interval_ms": interval, "concurrency": concurrency}},
    )[source]
    return PerformanceSettings(**values)


class RequestRateLimiter:
    """Thread-safe minimum spacing between request start times."""

    def __init__(self, interval_ms: int) -> None:
        self.interval_seconds = interval_ms / 1000.0
        self._lock = threading.Lock()
        self._next_request_at = 0.0

    def wait(self, checkpoint: Optional[Callable[[], None]] = None) -> None:
        if self.interval_seconds <= 0:
            return
        with self._lock:
            now = time.monotonic()
            request_at = max(now, self._next_request_at)
            self._next_request_at = request_at + self.interval_seconds
        delay = request_at - now
        while delay > 0:
            time.sleep(min(delay, 0.5))
            if checkpoint is not None:
                checkpoint()
            delay = request_at - time.monotonic()
