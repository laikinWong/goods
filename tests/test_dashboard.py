import json
import io
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

from dashboard import Handler, Manager, tail, WINDOWS
from run_crawlers import CrawlerSpec


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = Manager(self.root)

    def tearDown(self):
        for job in self.manager.jobs.values():
            process = job.get('process')
            if process and process.poll() is None:
                self.manager.stop(job['id'])
                process.wait(timeout=8)
        self.tmp.cleanup()

    def await_done(self, job):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            with self.manager.lock:
                if job['status'] not in ('running', 'paused', 'finishing', 'stopping'):
                    return
            time.sleep(.02)
        self.fail('Job did not finish')

    def spec(self, source):
        script = self.root / 'fake.py'
        script.write_text(source, encoding="utf-8")
        return CrawlerSpec('sjyx', [sys.executable, str(script)], Path('result.json'))

    def test_success_streams_logs_and_copies_result(self):
        spec = self.spec("from pathlib import Path\nprint('商品已抓取', flush=True)\nPath('result.json').write_text('[1]')\n")
        with patch.dict('dashboard.CRAWLERS', sjyx=spec):
            job_id = self.manager.start(['sjyx'])[0]
            job = self.manager.jobs[job_id]
            self.await_done(job)
        self.assertEqual(job['status'], 'success')
        self.assertEqual(Path(job['output']).read_text(), '[1]')
        self.assertIn('商品已抓取', self.manager.snapshot()['jobs'][0]['text'])
        self.assertTrue((Path(job['log']).parent / ('control-' + job_id + '.json')).exists())

    def test_single_platform_uses_shared_catalog_database(self):
        spec = self.spec(
            "import json, os\n"
            "from pathlib import Path\n"
            "Path('result.json').write_text(json.dumps({"
            "'database': os.environ['GOODS_CATALOG_DB'],"
            "'interval': os.environ['GOODS_REQUEST_INTERVAL_MS'],"
            "'concurrency': os.environ['GOODS_CONCURRENCY']}))\n"
        )
        performance = {'sjyx': {'request_interval_ms': 750, 'concurrency': 4}}
        with patch.dict('dashboard.CRAWLERS', sjyx=spec):
            job = self.manager.jobs[self.manager.start(['sjyx'], performance=performance)[0]]
            self.await_done(job)

        values = json.loads(Path(job['output']).read_text())
        self.assertEqual(values['database'], str((self.root / 'data' / 'catalog.sqlite3').resolve()))
        self.assertEqual(values['interval'], '750')
        self.assertEqual(values['concurrency'], '4')
        self.assertEqual(job['performance'], performance['sjyx'])

    def test_performance_settings_are_validated_before_start(self):
        invalid = [
            {'sjyx': {'request_interval_ms': -1, 'concurrency': 1}},
            {'sjyx': {'request_interval_ms': 1, 'concurrency': 0}},
            {'sjyx': {'request_interval_ms': 1.5, 'concurrency': 1}},
            {'bdt': {'request_interval_ms': 100, 'concurrency': 1}},
        ]
        for performance in invalid:
            with self.subTest(performance=performance), self.assertRaises(ValueError):
                self.manager.start(['sjyx'], performance=performance)

    @unittest.skipIf(WINDOWS, "Unix signal behavior; Windows has dedicated control tests")
    def test_duplicate_start_rejected_and_stop_terminates_process(self):
        spec = self.spec('import time\nprint("ready", flush=True)\ntime.sleep(60)\n')
        with patch.dict('dashboard.CRAWLERS', sjyx=spec):
            job = self.manager.jobs[self.manager.start(['sjyx'])[0]]
            with self.assertRaisesRegex(ValueError, '重复启动'):
                self.manager.start(['sjyx'])
            self.manager.stop(job['id'])
            self.await_done(job)
        self.assertEqual(job['status'], 'finished')
        self.assertIsNotNone(job['process'].poll())

    def test_nonzero_exit_and_missing_output_are_failures(self):
        for source in ['raise SystemExit(7)', "print('no output')"]:
            spec = self.spec(source)
            with patch.dict('dashboard.CRAWLERS', sjyx=spec):
                job = self.manager.jobs[self.manager.start(['sjyx'])[0]]
                self.await_done(job)
                self.assertEqual(job['status'], 'failed')

    @unittest.skipIf(WINDOWS, "Unix signal behavior; Windows has dedicated control tests")
    def test_restores_cli_and_validates_external_process(self):
        folder = self.root / 'logs' / 'run-example'
        folder.mkdir(parents=True)
        (folder / 'orchestrator.log').write_text('[sjyx] started pid=123\n')
        self.manager.restore()
        with patch('dashboard.process_matches', return_value=True):
            job = self.manager.snapshot()['jobs'][0]
            self.assertEqual(job['status'], 'running')
            with self.assertRaisesRegex(ValueError, '重复启动'):
                self.manager.start(['sjyx'])
            with patch('dashboard.os.kill') as send_signal:
                self.manager.pause(job['id'])
                self.assertEqual(self.manager.jobs[job['id']]['status'], 'paused')
                send_signal.assert_called_once()
                self.manager.resume(job['id'])
        with patch('dashboard.process_matches', return_value=False):
            self.assertEqual(self.manager.snapshot()['jobs'][0]['status'], 'unknown')

    def test_invalid_targets_and_bounded_logs(self):
        for value in [[], None, ['invalid'], [['sjyx']], 'sjyx']:
            with self.assertRaises(ValueError):
                self.manager.start(value)
        path = self.root / 'log.txt'
        path.write_text('旧日志\n' * 10000 + '最新日志\n')
        self.assertLessEqual(len(tail(path, 100).encode()), 100)
        self.assertTrue(tail(path, 100).endswith('最新日志\n'))

    def test_crawler_cannot_start_while_same_platform_is_processing(self):
        with patch.object(self.manager.cloud_processing, 'active_targets', return_value={'sjyx'}):
            with self.assertRaisesRegex(ValueError, '云商品数据处理'):
                self.manager.start(['sjyx'])

    def test_spawn_failure_is_recorded(self):
        with patch('dashboard.subprocess.Popen', side_effect=OSError('missing executable')):
            job = self.manager.jobs[self.manager.start(['sjyx'])[0]]
        self.assertEqual(job['status'], 'failed')
        self.assertIn('启动失败', tail(job['log']))

    @unittest.skipIf(WINDOWS, "Unix signal behavior; Windows has dedicated control tests")
    def test_multiple_platforms_start_concurrently(self):
        spec = self.spec('import time\ntime.sleep(60)\n')
        specs = {name: CrawlerSpec(name, spec.command, spec.primary_output) for name in ['sjyx', 'lcgt', 'bdt']}
        with patch.dict('dashboard.CRAWLERS', specs):
            ids = self.manager.start(['sjyx', 'lcgt', 'bdt'])
            self.assertEqual(len(ids), 3)
            self.assertTrue(all(self.manager.jobs[i]['process'].poll() is None for i in ids))
            for job_id in ids:
                self.manager.stop(job_id)
            for job_id in ids:
                self.await_done(self.manager.jobs[job_id])

    @unittest.skipIf(WINDOWS, "Unix signal behavior; Windows has dedicated control tests")
    def test_pause_resume_then_finish_paused_process_saves_memory(self):
        spec = self.spec('''import json, signal, time
from pathlib import Path
done = False
def finish(*args):
    global done
    done = True
signal.signal(signal.SIGTERM, finish)
records = []
print('ready', flush=True)
while not done:
    records.append({'id': len(records)})
    Path('heartbeat').write_text(str(len(records)))
    time.sleep(.02)
path = Path('sjyx/outputs/full/products.jsonl')
path.parent.mkdir(parents=True)
path.write_text(''.join(json.dumps(row) + '\\n' for row in records))
''')
        with patch.dict('dashboard.CRAWLERS', sjyx=spec):
            job = self.manager.jobs[self.manager.start(['sjyx'])[0]]
            deadline = time.monotonic() + 3
            while not (self.root / 'heartbeat').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.root / 'heartbeat').exists())
            self.manager.pause(job['id'])
            time.sleep(.08)
            before = (self.root / 'heartbeat').read_text()
            time.sleep(.1)
            self.assertEqual(before, (self.root / 'heartbeat').read_text())
            with self.assertRaises(ValueError):
                self.manager.start(['sjyx'])
            self.manager.resume(job['id'])
            time.sleep(.1)
            self.assertGreater(int((self.root / 'heartbeat').read_text()), int(before))
            self.manager.pause(job['id'])
            self.manager.finish(job['id'])
            self.await_done(job)
        self.assertEqual(job['status'], 'finished')
        self.assertEqual(len(json.loads(Path(job['output']).read_text())), job['exported_count'])
        self.assertGreater(job['exported_count'], 0)
        restored = Manager(self.root)
        self.assertEqual(restored.jobs[job['id']]['output'], job['output'])

    def test_corrupt_partial_is_reported_and_preserved(self):
        path = self.root / 'bdt/data/goods_details_partial.json'
        path.parent.mkdir(parents=True)
        path.write_text('[broken')
        log = self.root / 'logs/run-test/bdt.log'
        log.parent.mkdir(parents=True)
        job = dict(id='test', target='bdt', log=str(log), graceful=True)
        self.manager.complete_finish(job)
        self.assertEqual(job['status'], 'failed')
        self.assertIn('export_error', job)
        self.assertEqual(path.read_text(), '[broken')

    def test_export_each_platform_and_empty_result(self):
        cases = [('sjyx', 'sjyx/outputs/full/products.jsonl', '{"id":1}\n', 1),
                 ('bdt', 'bdt/data/goods_details_partial.json', '[{"id":2}]', 1),
                 ('lcgt', 'lcgt/lcgt_products.json', '{"钢":{"板":{"products":[{"id":3}]}}}', 1)]
        for target, relative, content, count in cases:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            job = dict(target=target, graceful=True)
            self.manager.export_partial(job)
            self.assertEqual(job['exported_count'], 0)
            path.write_text(content)
            self.manager.export_partial(job)
            self.assertEqual(job['exported_count'], count)


class HTTPValidationTests(unittest.TestCase):
    def handler(self, body, token='valid', host='127.0.0.1:8765'):
        handler = Handler.__new__(Handler)
        payload = json.dumps(body).encode()
        handler.headers = {'Host': host, 'X-Control-Token': token, 'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        handler.server = Mock(server_port=8765, token='valid')
        handler.send = Mock()
        handler.path = '/api/start'
        return handler

    def test_cross_site_request_cannot_start_a_process(self):
        for token, host in [('', '127.0.0.1:8765'), ('valid', 'evil.example')]:
            handler = self.handler({'targets': ['sjyx']}, token, host)
            handler.do_POST()
            self.assertEqual(handler.send.call_args.args[0], 403)
            handler.server.manager.start.assert_not_called()

    def test_valid_start_and_invalid_body(self):
        performance = {'sjyx': {'request_interval_ms': 800, 'concurrency': 2}}
        handler = self.handler({'targets': ['sjyx'], 'dry_run': True, 'performance': performance})
        handler.server.manager.start.return_value = ['job-1']
        handler.do_POST()
        handler.server.manager.start.assert_called_once_with(['sjyx'], True, performance)
        self.assertEqual(handler.send.call_args.args[0], 200)
        handler = self.handler([])
        handler.do_POST()
        self.assertEqual(handler.send.call_args.args[0], 400)

    def test_pause_resume_finish_routes(self):
        for operation in ['pause', 'resume', 'finish']:
            handler = self.handler({'id':'job-1'})
            handler.path = '/api/' + operation
            handler.do_POST()
            getattr(handler.server.manager, operation).assert_called_once_with('job-1')
            self.assertEqual(handler.send.call_args.args[0], 200)

    def test_cloud_processing_route(self):
        handler = self.handler({'targets': ['bdt', 'sjyx'], 'mode': 'incremental'})
        handler.path = '/api/cloud-processing/start'
        handler.server.manager.start_cloud_processing.return_value = 'processing-1'
        handler.do_POST()
        handler.server.manager.start_cloud_processing.assert_called_once_with(['bdt', 'sjyx'], 'incremental')
        self.assertEqual(handler.send.call_args.args[0], 200)

    def test_cloud_processing_has_a_dedicated_page(self):
        handler = self.handler({})
        handler.path = '/cloud-products'
        handler.do_GET()
        status, body, content_type = handler.send.call_args.args
        self.assertEqual(status, 200)
        self.assertIn('text/html', content_type)
        self.assertIn('云商品数据对接'.encode(), body)
        self.assertIn(b'id="processing-start"', body)

        index = (Path(__file__).parents[1] / 'web' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('href="/cloud-products"', index)
        self.assertNotIn('id="processing-start"', index)

        app = (Path(__file__).parents[1] / 'web' / 'app.js').read_text(encoding='utf-8')
        self.assertIn('请求间隔（毫秒）', app)
        self.assertIn('并发数量', app)
        self.assertIn('performancePayload', app)


if __name__ == '__main__':
    unittest.main()
