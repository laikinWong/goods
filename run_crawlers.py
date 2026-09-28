#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import datetime as dt
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, TextIO


@dataclass(frozen=True)
class CrawlerSpec:
    name: str
    command: List[str]
    primary_output: Optional[Path] = None
    output_name: Optional[str] = None


@dataclass
class CrawlerResult:
    name: str
    command: List[str]
    log_path: Path
    exit_code: int
    duration_seconds: float
    final_output_path: Optional[Path] = None
    dry_run: bool = False


@dataclass
class RunResult:
    items: List[CrawlerResult]
    run_log_dir: Path
    exit_code: int


PYTHON = sys.executable or "python3"
CRAWLERS = {
    "sjyx": CrawlerSpec(
        "sjyx",
        [PYTHON, "sjyx/sjyx_crawler.py", "--output-dir", "sjyx/outputs/full", "--export-json"],
        Path("sjyx/outputs/full/products.json"),
    ),
    "lcgt": CrawlerSpec("lcgt", [PYTHON, "lcgt/lcgt_crawler.py"], Path("lcgt/lcgt_products.json")),
    "bdt": CrawlerSpec(
        "bdt",
        [PYTHON, "bdt/scraper.py"],
        Path("bdt/data/goods_details_all.json"),
    ),
}
CRAWLER_ORDER = list(CRAWLERS)


def expand_targets(targets: Sequence[str]) -> List[str]:
    expanded: List[str] = []
    for target in targets:
        names = CRAWLER_ORDER if target == "all" else [target]
        for name in names:
            if name not in CRAWLERS:
                valid = ", ".join(["all", *CRAWLER_ORDER])
                raise ValueError(f"Unknown crawler target: {name}. Valid targets: {valid}")
            if name not in expanded:
                expanded.append(name)
    return expanded


def run(
    targets: Sequence[str],
    *,
    parallel: bool = False,
    dry_run: bool = False,
    quiet: bool = False,
    continue_on_error: bool = False,
    log_dir: Path = Path("logs"),
    data_dir: Path = Path("data"),
    run_timestamp: Optional[str] = None,
) -> RunResult:
    selected = expand_targets(targets)
    run_log_dir = create_run_log_dir(log_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    output_timestamp = run_timestamp or dt.datetime.now().strftime("%Y%m%d%H%M%S")
    orchestrator_log_path = run_log_dir / "orchestrator.log"
    log_lock = threading.Lock()

    with orchestrator_log_path.open("a", encoding="utf-8") as orchestrator_log:
        log_event(
            orchestrator_log,
            log_lock,
            (
                f"targets={','.join(selected)} parallel={parallel} dry_run={dry_run} "
                f"data_dir={data_dir} output_timestamp={output_timestamp}"
            ),
        )
        if dry_run:
            results = [
                dry_run_crawler(
                    CRAWLERS[name],
                    run_log_dir,
                    data_dir,
                    output_timestamp,
                    orchestrator_log,
                    log_lock,
                    quiet,
                )
                for name in selected
            ]
        elif parallel:
            results = run_parallel(selected, run_log_dir, data_dir, output_timestamp, orchestrator_log, log_lock, quiet)
        else:
            results = run_sequential(
                selected,
                run_log_dir,
                data_dir,
                output_timestamp,
                orchestrator_log,
                log_lock,
                quiet,
                continue_on_error,
            )

    exit_code = 0 if all(item.exit_code == 0 for item in results) else 1
    result = RunResult(items=results, run_log_dir=run_log_dir, exit_code=exit_code)
    print_summary(result)
    return result


def create_run_log_dir(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().strftime("run-%Y%m%d-%H%M%S")
    candidate = log_dir / timestamp
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = log_dir / f"{timestamp}-{suffix}"
    candidate.mkdir(parents=True)
    return candidate


def dry_run_crawler(
    spec: CrawlerSpec,
    run_log_dir: Path,
    data_dir: Path,
    output_timestamp: str,
    orchestrator_log: TextIO,
    log_lock: threading.Lock,
    quiet: bool,
) -> CrawlerResult:
    start = time.monotonic()
    log_path = run_log_dir / f"{spec.name}.log"
    final_output_path = build_final_output_path(spec.output_name or spec.name, data_dir, output_timestamp)
    message = "DRY RUN command: " + " ".join(spec.command)
    output_message = f"DRY RUN final output: {final_output_path}"
    database_message = f"DRY RUN catalog database: {(data_dir / 'catalog.sqlite3').resolve()}"
    log_path.write_text(message + "\n" + output_message + "\n" + database_message + "\n", encoding="utf-8")
    log_event(
        orchestrator_log,
        log_lock,
        f"[{spec.name}] dry_run command={' '.join(spec.command)} final_output={final_output_path}",
    )
    if not quiet:
        print_prefixed(spec.name, message)
        print_prefixed(spec.name, output_message)
        print_prefixed(spec.name, database_message)
    return CrawlerResult(
        name=spec.name,
        command=spec.command,
        log_path=log_path,
        exit_code=0,
        duration_seconds=time.monotonic() - start,
        final_output_path=final_output_path,
        dry_run=True,
    )


def run_sequential(
    selected: Sequence[str],
    run_log_dir: Path,
    data_dir: Path,
    output_timestamp: str,
    orchestrator_log: TextIO,
    log_lock: threading.Lock,
    quiet: bool,
    continue_on_error: bool,
) -> List[CrawlerResult]:
    results: List[CrawlerResult] = []
    for name in selected:
        result = run_one(CRAWLERS[name], run_log_dir, data_dir, output_timestamp, orchestrator_log, log_lock, quiet)
        results.append(result)
        if result.exit_code != 0 and not continue_on_error:
            log_event(orchestrator_log, log_lock, f"[{name}] stopping sequential run after failure")
            break
    return results


def run_parallel(
    selected: Sequence[str],
    run_log_dir: Path,
    data_dir: Path,
    output_timestamp: str,
    orchestrator_log: TextIO,
    log_lock: threading.Lock,
    quiet: bool,
) -> List[CrawlerResult]:
    results_by_name: dict[str, CrawlerResult] = {}
    result_lock = threading.Lock()

    def worker(name: str) -> None:
        result = run_one(CRAWLERS[name], run_log_dir, data_dir, output_timestamp, orchestrator_log, log_lock, quiet)
        with result_lock:
            results_by_name[name] = result

    threads = [threading.Thread(target=worker, args=(name,), daemon=False) for name in selected]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return [results_by_name[name] for name in selected]


def run_one(
    spec: CrawlerSpec,
    run_log_dir: Path,
    data_dir: Path,
    output_timestamp: str,
    orchestrator_log: TextIO,
    log_lock: threading.Lock,
    quiet: bool,
) -> CrawlerResult:
    start = time.monotonic()
    log_path = run_log_dir / f"{spec.name}.log"
    log_event(orchestrator_log, log_lock, f"[{spec.name}] starting command={' '.join(spec.command)}")
    if not quiet:
        print_prefixed(spec.name, "started")

    with log_path.open("w", encoding="utf-8") as child_log:
        process = subprocess.Popen(
            spec.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env={
                **os.environ,
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
                "GOODS_CATALOG_DB": str((data_dir / "catalog.sqlite3").resolve()),
            },
        )
        log_event(orchestrator_log, log_lock, f"[{spec.name}] started pid={process.pid}")
        if process.stdout is not None:
            for line in process.stdout:
                child_log.write(line)
                child_log.flush()
                if not quiet:
                    print_prefixed(spec.name, line.rstrip("\n"))
            process.stdout.close()
        exit_code = process.wait()

    duration = time.monotonic() - start
    final_output_path: Optional[Path] = None
    if exit_code == 0 and spec.primary_output is not None:
        try:
            final_output_path = copy_primary_output(spec, data_dir, output_timestamp)
            log_event(orchestrator_log, log_lock, f"[{spec.name}] copied output to {final_output_path}")
        except OSError as exc:
            exit_code = 1
            log_event(orchestrator_log, log_lock, f"[{spec.name}] output copy failed: {exc}")

    log_event(
        orchestrator_log,
        log_lock,
        f"[{spec.name}] finished exit_code={exit_code} duration={format_duration(duration)} log={log_path}",
    )
    if not quiet:
        print_prefixed(spec.name, f"finished exit_code={exit_code}")
    return CrawlerResult(
        name=spec.name,
        command=spec.command,
        log_path=log_path,
        exit_code=exit_code,
        duration_seconds=duration,
        final_output_path=final_output_path,
    )


def build_final_output_path(name: str, data_dir: Path, output_timestamp: str) -> Path:
    return data_dir / f"{name}-{output_timestamp}.json"


def copy_primary_output(spec: CrawlerSpec, data_dir: Path, output_timestamp: str) -> Path:
    if spec.primary_output is None:
        raise OSError(f"No primary output configured for {spec.name}")
    if not spec.primary_output.exists():
        raise FileNotFoundError(f"Primary output not found: {spec.primary_output}")
    data_dir.mkdir(parents=True, exist_ok=True)
    final_output_path = build_final_output_path(spec.output_name or spec.name, data_dir, output_timestamp)
    shutil.copyfile(spec.primary_output, final_output_path)
    return final_output_path


def log_event(handle: TextIO, lock: threading.Lock, message: str) -> None:
    with lock:
        handle.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {message}\n")
        handle.flush()


def print_prefixed(name: str, message: str) -> None:
    now = dt.datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] [{name}] {message}", flush=True)


def print_summary(result: RunResult) -> None:
    print("\nSummary:")
    for item in result.items:
        status = "success" if item.exit_code == 0 else "failed"
        if item.dry_run:
            status = "dry-run"
        output = f" output={item.final_output_path}" if item.final_output_path else ""
        print(
            f"  {item.name:<8} {status:<8} {format_duration(item.duration_seconds):>8}  {item.log_path}{output}"
        )
    print(f"Logs: {result.run_log_dir}")


def format_duration(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one or more goods crawler scripts.")
    parser.add_argument("--target", nargs="+", required=True, help="Crawler target: sjyx, lcgt, bdt, or all")
    parser.add_argument("--parallel", action="store_true", help="Run selected crawlers concurrently")
    parser.add_argument("--dry-run", action="store_true", help="Print planned commands without launching crawlers")
    parser.add_argument("--quiet", action="store_true", help="Do not mirror child process output to the terminal")
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="In sequential mode, continue after a crawler exits non-zero",
    )
    parser.add_argument("--log-dir", default=Path("logs"), type=Path, help="Base directory for run logs")
    parser.add_argument("--data-dir", default=Path("data"), type=Path, help="Directory for timestamped crawler output files")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(
            targets=args.target,
            parallel=args.parallel,
            dry_run=args.dry_run,
            quiet=args.quiet,
            continue_on_error=args.continue_on_error,
            log_dir=args.log_dir,
            data_dir=args.data_dir,
        )
    except ValueError as exc:
        print(f"{parser.prog}: error: {exc}", file=sys.stderr)
        return 2
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
