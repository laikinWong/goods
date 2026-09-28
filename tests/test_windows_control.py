import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import crawler_control as control
import dashboard
from dashboard import Manager
from run_crawlers import CrawlerSpec


class WindowsControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = Manager(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_windows_actions_use_psutil_and_finish_file(self):
        folder = self.root / 'logs/run-test'
        folder.mkdir(parents=True)
        process = Mock()
        process.poll.return_value = None
        job = dict(id='test', pid=123, target='sjyx', process=process, owned=True,
                   status='running', created=42, log=str(folder / 'sjyx.log'),
                   finish_file=str(folder / 'finish.request'))
        self.manager.jobs['test'] = job
        native = Mock()
        native.create_time.return_value = 42
        psutil = Mock(Process=Mock(return_value=native), Error=RuntimeError)
        with patch.object(dashboard, 'WINDOWS', True), \
             patch.object(dashboard, 'windows_psutil', return_value=psutil), \
             patch.object(self.manager, 'signal_job') as unix_signal:
            self.manager.pause('test')
            native.suspend.assert_called_once()
            self.assertEqual(job['status'], 'paused')
            self.manager.resume('test')
            native.resume.assert_called_once()
            self.manager.pause('test')
            self.manager.finish('test')
            self.assertTrue(Path(job['finish_file']).exists())
            self.assertEqual(job['status'], 'finishing')
            self.assertEqual(native.resume.call_count, 2)
            native.terminate.assert_not_called()
            unix_signal.assert_not_called()

    def test_windows_pid_reuse_is_rejected(self):
        native = Mock()
        native.create_time.return_value = 99
        psutil = Mock(Process=Mock(return_value=native), Error=RuntimeError)
        with patch.object(dashboard, 'windows_psutil', return_value=psutil):
            with self.assertRaisesRegex(ValueError, 'PID'):
                self.manager.windows_action({'pid':123, 'created':42}, 'suspend')
        native.suspend.assert_not_called()

    def test_windows_launch_uses_process_group_utf8_and_control_channel(self):
        child = Mock(pid=123)
        native = Mock()
        native.create_time.return_value = 42.0
        psutil = Mock(Process=Mock(return_value=native), Error=RuntimeError)
        with patch.object(dashboard, 'WINDOWS', True), \
             patch.object(dashboard, 'windows_psutil', return_value=psutil), \
             patch.object(subprocess, 'CREATE_NEW_PROCESS_GROUP', 512, create=True), \
             patch.object(subprocess, 'Popen', return_value=child) as launch, \
             patch.object(dashboard.threading, 'Thread'):
            job = self.manager.jobs[self.manager.start(['sjyx'])[0]]
        options = launch.call_args.kwargs
        self.assertEqual(options['creationflags'], 512)
        self.assertNotIn('start_new_session', options)
        self.assertEqual(options['env']['PYTHONIOENCODING'], 'utf-8')
        self.assertEqual(options['env']['GOODS_FINISH_FILE'], job['finish_file'])
        self.assertEqual(job['created'], 42.0)
        restored = Manager(self.root).jobs[job['id']]
        self.assertEqual(restored['finish_file'], job['finish_file'])
        self.assertEqual(restored['created'], 42.0)

    def test_windows_process_matching_handles_paths_and_exited_processes(self):
        native = Mock()
        native.create_time.return_value = 42
        native.cmdline.return_value = ['python.exe', 'C:\\项目\\sjyx\\sjyx_crawler.py']
        psutil = Mock(Process=Mock(return_value=native), Error=RuntimeError)
        with patch.object(dashboard, 'WINDOWS', True), patch.object(dashboard, 'windows_psutil', return_value=psutil):
            self.assertTrue(dashboard.process_matches(123, 'sjyx', 42))
            self.assertFalse(dashboard.process_matches(123, 'sjyx', 99))
            psutil.Process.side_effect = RuntimeError('no process')
            self.assertFalse(dashboard.process_matches(123, 'sjyx', 42))

    def test_finish_file_wakes_checkpoint_without_signal(self):
        request = self.root / 'finish.request'
        with patch.object(control, '_finish_file', request), patch.object(control, '_finishing', False):
            control.checkpoint()
            request.touch()
            with self.assertRaises(control.FinishRequested):
                control.checkpoint()

    def test_real_process_saves_data_using_file_finish(self):
        script = self.root / '中文 测试.py'
        script.write_text('''import sys, time
from pathlib import Path
sys.path.insert(0, %r)
import crawler_control as control
control.install()
rows = []
try:
    while True:
        control.checkpoint()
        rows.append('{"name":"测试商品"}')
        print('中文日志正常', flush=True)
        time.sleep(.02)
except control.FinishRequested:
    pass
finally:
    p = Path('sjyx/outputs/full/products.jsonl')
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('\\n'.join(rows), encoding='utf-8')
''' % str(dashboard.ROOT), encoding='utf-8')
        spec = CrawlerSpec('sjyx', [sys.executable, str(script)], Path('sjyx/outputs/full/products.json'))
        with patch.dict(dashboard.CRAWLERS, sjyx=spec):
            job = self.manager.jobs[self.manager.start(['sjyx'])[0]]
            try:
                deadline = time.monotonic() + 5
                while '中文日志正常' not in dashboard.tail(job['log']) and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertIn('中文日志正常', dashboard.tail(job['log']))
                self.manager.pause(job['id'])
                time.sleep(.08)
                paused_log = dashboard.tail(job['log'])
                time.sleep(.08)
                self.assertEqual(paused_log, dashboard.tail(job['log']))
                self.manager.resume(job['id'])
                time.sleep(.1)
                self.assertGreater(len(dashboard.tail(job['log'])), len(paused_log))
                # Exercise the Windows finish path on any host, with a real child.
                with patch.object(dashboard, 'WINDOWS', True):
                    self.manager.finish(job['id'])
                job['process'].wait(timeout=8)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    with self.manager.lock:
                        if job['status'] != 'finishing':
                            break
                    time.sleep(.02)
                self.assertEqual(job['status'], 'finished')
                self.assertEqual(job['exit_code'], 0)
                self.assertGreater(job['exported_count'], 0)
                self.assertIn('测试商品', Path(job['output']).read_text(encoding='utf-8'))
            finally:
                if job['process'].poll() is None:
                    job['process'].kill()
                    job['process'].wait(timeout=5)
