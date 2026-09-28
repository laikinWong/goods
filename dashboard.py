#!/usr/bin/env python3
"""Local crawler control panel. Run with python3 dashboard.py."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import datetime as dt
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from category_statistics import export_category_statistics
from cloud_product_processing import CloudProductProcessor
from crawler_performance import PERFORMANCE_DEFAULTS, validate_performance_settings
from run_crawlers import CRAWLERS, copy_primary_output, create_run_log_dir

ROOT = Path(__file__).resolve().parent
WINDOWS = sys.platform == "win32"
ACTIVE = ("running", "paused", "finishing", "stopping")
LABELS = {"sjyx": "中建三局严选", "lcgt": "乐从钢铁", "bdt": "八达通"}


def windows_psutil():
    try:
        import psutil
        return psutil
    except ImportError:
        raise ValueError("Windows 进程管理需要 psutil，请运行 python -m pip install -r requirements.txt")


def process_matches(pid, target, created=None):
    """Do not mistake a recycled PID for an active crawler."""
    if WINDOWS:
        psutil = windows_psutil()
        try:
            process = psutil.Process(pid)
            return (process.is_running()
                    and (created is None or process.create_time() == created)
                    and any(arg.replace('\\', '/').endswith(CRAWLERS[target].command[1].replace('\\', '/'))
                            for arg in process.cmdline()))
        except psutil.Error:
            return False
    try:
        command = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="], text=True, timeout=2
        )
        return CRAWLERS[target].command[1] in command
    except (OSError, subprocess.SubprocessError):
        return False


def tail(path, limit=64000):
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            data = handle.read()
        if size > limit:
            data = data.partition(b"\n")[2]
        return data.decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


class Manager:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.jobs = {}
        self.cloud_processing = CloudProductProcessor(self.root)
        self.restore()

    def restore(self):
        # Import CLI runs, including the crawlers started before this panel.
        for folder in sorted((self.root / "logs").glob("run-*")):
            metadata = folder / "dashboard.json"
            if metadata.exists():
                try:
                    job = json.loads(metadata.read_text(encoding="utf-8"))
                    job["owned"] = False
                    job["process"] = None
                    self.jobs[job["id"]] = job
                except (ValueError, KeyError):
                    pass
                continue
            log = tail(folder / "orchestrator.log", 200000)
            for name, pid in re.findall(r"\[(sjyx|lcgt|bdt)\] started pid=(\d+)", log):
                finished = re.search(r"\[" + name + r"\] finished exit_code=(-?\d+)", log)
                code = int(finished[1]) if finished else None
                job_id = folder.name + "-" + name
                self.jobs[job_id] = dict(
                    id=job_id, target=name, pid=int(pid), owned=False, process=None,
                    status=("success" if code == 0 else "failed") if finished else "running",
                    exit_code=code, started=folder.stat().st_mtime, ended=None,
                    log=str(folder / (name + ".log")), output=None,
                    command=" ".join(CRAWLERS[name].command),
                )

        for metadata in (self.root / "logs").glob("run-*/control-*.json"):
            try:
                job = json.loads(metadata.read_text(encoding="utf-8"))
                job.update(owned=False, process=None)
                self.jobs[job["id"]] = job
            except (ValueError, KeyError):
                pass

    def refresh(self):
        for job in self.jobs.values():
            if not job["owned"] and job["status"] in ACTIVE:
                if not process_matches(job["pid"], job["target"], job.get("created")):
                    if job["status"] == "finishing":
                        self.complete_finish(job)
                        continue
                    log = tail(Path(job["log"]).parent / "orchestrator.log")
                    result = re.search(r"\[" + job["target"] + r"\] finished exit_code=(-?\d+)", log)
                    job["exit_code"] = int(result[1]) if result else None
                    job["status"] = ("success" if job["exit_code"] == 0 else "failed") if result else "unknown"

    def persist(self, job):
        path = Path(job["log"]).parent / ("control-" + job["id"] + ".json")
        value = {k: v for k, v in job.items() if k != "process"}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def snapshot(self):
        with self.lock:
            self.refresh()
            items = []
            for job in sorted(self.jobs.values(), key=lambda item: item["started"], reverse=True):
                item = {k: v for k, v in job.items() if k != "process"}
                item["text"] = tail(job["log"])
                items.append(item)
            return {
                "jobs": items,
                "platforms": LABELS,
                "performance_defaults": PERFORMANCE_DEFAULTS,
                "cloud_processing": self.cloud_processing.snapshot(),
            }

    def start_cloud_processing(self, targets, mode):
        with self.lock:
            self.refresh()
            selected = set(targets) if isinstance(targets, list) else set()
            busy = {
                job["target"] for job in self.jobs.values()
                if job["status"] in ACTIVE and job["target"] in selected
            }
            if busy:
                raise ValueError("请先等待所选平台完成增量爬取：" + "、".join(LABELS[target] for target in LABELS if target in busy))
        return self.cloud_processing.start(targets, mode)

    def export_category_statistics(self):
        with self.lock:
            return export_category_statistics(self.root)

    def start(self, targets, dry_run=False, performance=None):
        if WINDOWS:
            windows_psutil()
        if not isinstance(targets, list) or not targets or any(not isinstance(t, str) or t not in CRAWLERS for t in targets):
            raise ValueError("请选择有效的平台")
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run 必须为布尔值")
        targets = list(dict.fromkeys(targets))
        performance = validate_performance_settings(targets, performance)
        with self.lock:
            self.refresh()
            processing_targets = self.cloud_processing.active_targets()
            if processing_targets.intersection(targets):
                raise ValueError("所选平台正在进行云商品数据处理，请等待处理完成")
            busy = {j["target"] for j in self.jobs.values() if j["status"] in ACTIVE}
            if busy.intersection(targets):
                raise ValueError("平台正在运行，请勿重复启动：" + "、".join(LABELS[t] for t in targets if t in busy))
            ids = []
            for target in targets:
                folder = create_run_log_dir(self.root / "logs")
                job_id = folder.name + "-" + target
                spec = CRAWLERS[target]
                command = [spec.command[0], "-u", *spec.command[1:]]
                if dry_run:
                    command = [spec.command[0], "-u", "run_crawlers.py", "--target", target, "--dry-run", "--log-dir", str(folder / "preview")]
                job = dict(id=job_id, target=target, pid=None, owned=True, process=None,
                           status="running", exit_code=None, started=time.time(), ended=None,
                           log=str(folder / (target + ".log")), output=None,
                           command=" ".join(command), dry_run=dry_run, graceful=True,
                           performance=performance[target])
                if not dry_run:
                    job["finish_file"] = str((folder / "finish.request").resolve())
                self.jobs[job_id] = job
                ids.append(job_id)
                with Path(job["log"]).open("w", encoding="utf-8") as output:
                    output.write("$ " + job["command"] + "\n")
                    output.flush()
                    try:
                        env = {
                            **os.environ,
                            "PYTHONUNBUFFERED": "1",
                            "PYTHONIOENCODING": "utf-8",
                            "PYTHONUTF8": "1",
                            "GOODS_CATALOG_DB": str((self.root / "data" / "catalog.sqlite3").resolve()),
                            "GOODS_REQUEST_INTERVAL_MS": str(performance[target]["request_interval_ms"]),
                            "GOODS_CONCURRENCY": str(performance[target]["concurrency"]),
                        }
                        env.pop("GOODS_FINISH_FILE", None)
                        if job.get("finish_file"):
                            env["GOODS_FINISH_FILE"] = job["finish_file"]
                        options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                                   if WINDOWS else {"start_new_session": True})
                        process = subprocess.Popen(command, cwd=self.root, stdout=output,
                            stderr=subprocess.STDOUT, env=env, **options)
                    except OSError as exc:
                        output.write("启动失败：" + str(exc) + "\n")
                        job.update(status="failed", ended=time.time())
                        self.persist(job)
                        continue
                job.update(process=process, pid=process.pid)
                if WINDOWS:
                    psutil = windows_psutil()
                    try:
                        job["created"] = psutil.Process(process.pid).create_time()
                    except psutil.Error:
                        pass  # A short dry-run may already have exited.
                self.persist(job)
                threading.Thread(target=self.wait, args=(job,), daemon=True).start()
            return ids

    def wait(self, job):
        code = job["process"].wait()
        with self.lock:
            if job["status"] == "finishing":
                job["exit_code"] = code
                self.complete_finish(job)
                return
            stopped = job["status"] == "stopping"
            job.update(exit_code=code, ended=time.time(), status="stopped" if stopped else ("success" if code == 0 else "failed"))
            if code == 0 and not stopped and not job["dry_run"]:
                try:
                    spec = CRAWLERS[job["target"]]
                    # CrawlerSpec uses relative paths; resolve against this project.
                    from dataclasses import replace
                    spec = replace(spec, primary_output=self.root / spec.primary_output)
                    stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S%f")
                    job["output"] = str(copy_primary_output(spec, self.root / "data", stamp))
                except OSError as exc:
                    job["status"] = "failed"
                    with Path(job["log"]).open("a", encoding="utf-8") as output:
                        output.write("\n结果汇总失败：" + str(exc) + "\n")
            self.persist(job)

    def controlled_job(self, job_id, states):
        job = self.jobs.get(job_id)
        if not job or job["status"] not in states:
            raise ValueError("当前任务状态不支持此操作")
        process = job.get("process")
        alive = process.poll() is None if process else process_matches(job["pid"], job["target"], job.get("created"))
        if not alive:
            raise ValueError("任务进程已结束，请刷新状态")
        return job

    def windows_action(self, job, action):
        psutil = windows_psutil()
        try:
            process = psutil.Process(job["pid"])
            if job.get("created") is not None and process.create_time() != job["created"]:
                raise ValueError("任务进程已退出，PID 已被其他进程使用")
            getattr(process, action)()
        except psutil.Error as exc:
            raise ValueError("无法控制 Windows 任务进程：" + str(exc))

    def signal_job(self, job, sig):
        # CLI crawlers share their parent's process group: signal only their PID.
        try:
            if job.get("owned") and job.get("process"):
                os.killpg(job["pid"], sig)
            else:
                os.kill(job["pid"], sig)
        except ProcessLookupError:
            raise ValueError("任务进程已结束，请刷新状态")

    def pause(self, job_id):
        with self.lock:
            job = self.controlled_job(job_id, ("running",))
            if WINDOWS:
                self.windows_action(job, "suspend")
            else:
                self.signal_job(job, signal.SIGSTOP)
            job["status"] = "paused"
            self.persist(job)

    def resume(self, job_id):
        with self.lock:
            job = self.controlled_job(job_id, ("paused",))
            if WINDOWS:
                self.windows_action(job, "resume")
            else:
                self.signal_job(job, signal.SIGCONT)
            job["status"] = "running"
            self.persist(job)

    def stop(self, job_id):
        # Retain the old API route as an alias for finish-and-export.
        return self.finish(job_id)

    def finish(self, job_id):
        with self.lock:
            job = self.controlled_job(job_id, ("running", "paused"))
            was_paused = job["status"] == "paused"
            if WINDOWS:
                if not job.get("finish_file"):
                    raise ValueError("此任务不支持保存后结束，请等待完成；Windows 正式采集请从新版控制台启动")
                # Windows terminate/SIGTERM forcibly kills Python, bypassing finalizers.
                # A per-run request file lets the crawler exit at its next checkpoint.
                Path(job["finish_file"]).touch()
                if was_paused:
                    self.windows_action(job, "resume")
            else:
                self.signal_job(job, signal.SIGTERM)
                if was_paused:
                    self.signal_job(job, signal.SIGCONT)
            job["status"] = "finishing"
            self.persist(job)
            if not job.get("process"):
                threading.Thread(target=self.wait_external, args=(job,), daemon=True).start()

    def wait_external(self, job):
        while process_matches(job["pid"], job["target"], job.get("created")):
            time.sleep(.5)
        with self.lock:
            if job["status"] == "finishing":
                self.complete_finish(job)

    def complete_finish(self, job):
        job.update(status="finished", ended=time.time())
        try:
            if not job.get("dry_run"):
                self.export_partial(job)
        except (OSError, ValueError) as exc:
            job.update(status="failed", export_error=str(exc))
        self.persist(job)

    def export_partial(self, job):
        target = job["target"]
        if target == "sjyx":
            source = self.root / "sjyx/outputs/full/products.jsonl"
            with source.open(encoding="utf-8") if source.exists() else nullcontext([]) as lines:
                data = [json.loads(line) for line in lines if line.strip()]
            count = len(data)
        elif target == "bdt":
            sources = [self.root / "bdt/data" / name for name in ("goods_details_partial.json", "goods_details_all.json")]
            sources = [path for path in sources if path.exists()]
            source = max(sources, key=lambda path: path.stat().st_mtime) if sources else None
            data = json.loads(source.read_text(encoding="utf-8")) if source else []
            count = len(data)
        else:
            source = self.root / "lcgt/lcgt_products.json"
            data = json.loads(source.read_text(encoding="utf-8")) if source.exists() else {}
            count = sum(len(sub.get("products", [])) for cats in data.values() for sub in cats.values())
        folder = self.root / "data"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S%f")
        output = folder / f"{target}-{stamp}-partial.json"
        tmp = output.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(output)
        job.update(output=str(output), exported_count=count,
                   export_note="已保存并汇总当前结果" if job.get("graceful") and job.get("exit_code") == 0 else "已汇总落盘数据；进程未确认正常保存退出")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, code, body, content_type="application/json; charset=utf-8"):
        data = json.dumps(body, ensure_ascii=False).encode() if isinstance(body, dict) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def trusted_host(self):
        return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def do_GET(self):
        if not self.trusted_host():
            return self.send(403, {"error": "仅允许本机访问"})
        path = urlsplit(self.path).path
        if path == "/api/state":
            return self.send(200, {**self.server.manager.snapshot(), "token": self.server.token})
        files = {
            "/": ("index.html", "text/html"),
            "/cloud-products": ("cloud-products.html", "text/html"),
            "/cloud-products.html": ("cloud-products.html", "text/html"),
            "/app.js": ("app.js", "text/javascript"),
            "/cloud-products.js": ("cloud-products.js", "text/javascript"),
            "/style.css": ("style.css", "text/css"),
        }
        if path in files:
            filename, kind = files[path]
            return self.send(200, (ROOT / "web" / filename).read_bytes(), kind + "; charset=utf-8")
        self.send(404, {"error": "未找到"})

    def do_POST(self):
        if not self.trusted_host() or not secrets.compare_digest(self.headers.get("X-Control-Token", ""), self.server.token):
            return self.send(403, {"error": "页面会话已失效，请刷新页面"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise ValueError("请求大小无效")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("请求格式无效")
            if self.path == "/api/start":
                ids = self.server.manager.start(
                    body.get("targets"),
                    body.get("dry_run", False),
                    body.get("performance"),
                )
                return self.send(200, {"ids": ids})
            if self.path == "/api/cloud-processing/start":
                job_id = self.server.manager.start_cloud_processing(body.get("targets"), body.get("mode"))
                return self.send(200, {"id": job_id})
            if self.path == "/api/category-statistics/export":
                return self.send(200, self.server.manager.export_category_statistics())
            if self.path in ("/api/stop", "/api/finish", "/api/pause", "/api/resume"):
                if not isinstance(body.get("id"), str):
                    raise ValueError("任务 ID 无效")
                method = {"/api/stop": "finish", "/api/finish": "finish", "/api/pause": "pause", "/api/resume": "resume"}[self.path]
                getattr(self.server.manager, method)(body["id"])
                return self.send(200, {"ok": True})
            return self.send(404, {"error": "未找到"})
        except (ValueError, UnicodeDecodeError) as exc:
            self.send(400, {"error": str(exc)})
        except OSError as exc:
            self.send(409, {"error": "操作未完成：" + str(exc)})


def main():
    parser = argparse.ArgumentParser(description="本地爬虫控制台")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if WINDOWS:
        windows_psutil()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.manager = Manager()
    server.token = secrets.token_urlsafe(32)
    print(f"爬虫控制台：http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n控制台已关闭；已启动的爬虫继续运行。", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
