"""Supervisor contract tests; no container runtime or network required."""
from pathlib import Path
import importlib.util
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
spec = importlib.util.spec_from_file_location('container_entrypoint', ROOT / 'deploy/container-entrypoint.py')
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)


class ContainerTests(unittest.TestCase):
    def setUp(self):
        entrypoint._stop = threading.Event()
        entrypoint._procs.clear()

    def test_python_build_tools_are_not_runtime_dependencies(self):
        containerfile = (ROOT / 'Containerfile').read_text()
        stages = containerfile.split('\nFROM ')
        builder = next(stage for stage in stages if stage.splitlines()[0].endswith(' AS python-builder'))
        runtime = stages[-1]
        self.assertEqual(builder.splitlines()[0].split()[0], runtime.splitlines()[0])
        for package in ('gcc', 'gcc-c++', 'make', 'python3-devel', 'libcurl-devel',
                        'openssl-devel', 'autoconf', 'automake', 'libtool'):
            self.assertIn(package, builder.split())
            self.assertNotIn(package, runtime.split())
        for package in ('python3', 'python3-rpm', 'rpm-build', 'python-rpm-macros',
                        'python3-rpm-macros', 'pyproject-rpm-macros',
                        'python3-rpm-generators', 'libcurl', 'openssl-libs', 'libseccomp', 'git', 'nodejs'):
            self.assertIn(package, runtime.split())
        self.assertIn('venv --system-site-packages /opt/venv', builder)
        self.assertIn('COPY --from=python-builder /opt/venv/ /opt/venv/', runtime)
        self.assertNotIn('pip install', runtime)

    def test_service_commands_have_importable_working_directories_and_start_before_clone(self):
        jobs = []
        class Thread:
            def __init__(self, *, target, args, daemon):
                jobs.append((target, args))
            def start(self):
                pass
            def join(self, timeout):
                pass
        config = {'collector': {'obs_interval_seconds': 60, 'nvchecker_interval_seconds': 3600},
                  'spec': {'repo': '/data/spec-full.git', 'url': 'https://example.invalid/spec.git',
                           'branch': 'main', 'interval_seconds': 3600}}
        entrypoint._stop.set()
        with patch.object(entrypoint, 'load_runtime', return_value=config), \
             patch.object(entrypoint.threading, 'Thread', Thread), \
             patch.object(entrypoint.os, 'makedirs'), patch.object(entrypoint.signal, 'signal'), \
             patch.object(entrypoint.subprocess, 'Popen') as popen, \
             patch.dict(os.environ, {'HOST': '127.0.0.2'}):
            entrypoint.main()
        self.assertEqual(jobs[0][1][0], 'api')
        self.assertEqual(jobs[0][1][-1], '/app/backend')
        self.assertEqual(jobs[1][1][0], 'web')
        self.assertEqual(jobs[1][1][-1], '/app/frontend')
        self.assertEqual(jobs[1][1][1], ['node', '/app/frontend/server.mjs'])
        self.assertEqual(jobs[1][1][2]['HOST'], '127.0.0.2')
        self.assertIn((entrypoint.specs, (config['spec'],)), jobs)
        self.assertIn((entrypoint.periodic, ("builds", 30)), jobs)
        self.assertIn((entrypoint.periodic, ("obs-metadata", 60)), jobs)
        popen.assert_not_called()  # Main itself never performs a blocking clone.

    def test_monitor_heartbeat_is_automatic_and_separate_from_recheck_interval(self):
        jobs = []
        class Thread:
            def __init__(self, *, target, args, daemon): jobs.append((target, args))
            def start(self): pass
            def join(self, timeout): pass
        config = {'collector': {'obs_interval_seconds':60, 'nvchecker_interval_seconds':3600},
                  'spec': {'repo':None}, 'monitors': {'enabled':['security'], 'interval_seconds':1800}}
        entrypoint._stop.set()
        with patch.object(entrypoint, 'load_runtime', return_value=config), \
             patch.object(entrypoint.threading, 'Thread', Thread), \
             patch.object(entrypoint.os, 'makedirs'), patch.object(entrypoint.signal, 'signal'):
            entrypoint.main()
        self.assertIn((entrypoint.periodic, ('monitors', 30)), jobs)

    def test_image_healthcheck_command_uses_runtime_host(self):
        line = next(line.strip() for line in (ROOT / 'Containerfile').read_text().splitlines()
                    if line.strip().startswith('CMD python3 -c '))
        command = shlex.split(line[len('CMD '):])
        self.assertEqual(command[:2], ['python3', '-c'])
        wrapper = "import io,sys,urllib.request; urllib.request.urlopen=lambda url,timeout:(print(url,timeout),io.BytesIO(b'ok'))[1]; exec(sys.argv[1])"
        cases = [(None, '127.0.0.1'), ('0.0.0.0', '127.0.0.1'), ('192.0.2.10', '192.0.2.10'), ('::1', '[::1]')]
        for host, expected in cases:
            env = {**os.environ, 'PORT': '18730'}
            env.pop('HOST', None)
            if host is not None:
                env['HOST'] = host
            result = subprocess.run([sys.executable, '-c', wrapper, command[2]],
                                    env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), f'http://{expected}:18730/livez 3')

    def test_collector_uses_external_config_and_bounded_batch(self):
        with patch.object(entrypoint, 'run_child', return_value=2) as child:
            entrypoint.run_collector('obs')
        args, kwargs = child.call_args
        self.assertEqual(args[0], 'collect-obs')
        self.assertIn(entrypoint.CONFIG, args[1])
        self.assertNotIn('--source-limit', args[1])
        self.assertEqual(kwargs['cwd'], '/app/backend')

    def test_build_poll_backoff_is_bounded_and_recovers_without_overlap(self):
        waits = []
        results = iter([2, 2, 2, 2, 2, 0, 75, 0])
        def wait(delay):
            waits.append(delay)
            if len(waits) == 8:
                entrypoint._stop.set()
        with patch.object(entrypoint, 'run_collector', side_effect=lambda _: next(results)), \
             patch.object(entrypoint._stop, 'wait', side_effect=wait):
            entrypoint.periodic('builds', 30)
        self.assertEqual(waits, [60, 120, 240, 300, 300, 30, 30, 30])

    def test_clone_uses_config_origin_and_branch(self):
        config = {'repo': '/data/spec.git', 'url': 'https://example.invalid/repo.git',
                  'branch': 'testing', 'interval_seconds': 800, 'fetch_timeout_seconds': 47}
        with patch.object(entrypoint, 'run_child', return_value=0) as child, \
             patch.object(entrypoint, 'periodic') as periodic:
            entrypoint.specs(config)
        env = child.call_args.kwargs['env']
        self.assertEqual((env['SPEC_REPO_DIR'], env['SPEC_REPO_URL'], env['SPEC_REPO_BRANCH']),
                         (config['repo'], config['url'], config['branch']))
        periodic.assert_called_once_with('specs', 800)
        self.assertEqual(child.call_args.kwargs['timeout'], 47)

    def test_init_timeout_terminates_child_and_retries_before_periodic(self):
        started = time.monotonic()
        result = entrypoint.run_child('spec-init-test',
            [sys.executable, '-c', 'import time; time.sleep(120)'], cwd=str(ROOT), timeout=0.1)
        self.assertEqual(result, 124)
        self.assertLess(time.monotonic() - started, 7)
        self.assertEqual(entrypoint._procs, {})
        config = {'repo': '/data/spec.git', 'url': 'https://example.invalid/repo.git',
                  'branch': 'main', 'interval_seconds': 800, 'fetch_timeout_seconds': 47}
        with patch.object(entrypoint, 'run_child', side_effect=[124, 0]) as child, \
             patch.object(entrypoint._stop, 'wait') as wait, \
             patch.object(entrypoint, 'periodic') as periodic:
            entrypoint.specs(config)
        self.assertEqual(child.call_count, 2)
        wait.assert_called_once_with(60)
        periodic.assert_called_once_with('specs', 800)

    def test_shutdown_stops_active_collector_process(self):
        started = threading.Event()
        original_popen = subprocess.Popen
        def popen(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            started.set()
            return process
        with patch.object(entrypoint.subprocess, 'Popen', side_effect=popen):
            thread = threading.Thread(target=entrypoint.run_child,
                                      args=('collect-test', [sys.executable, '-c', 'import time; time.sleep(120)']),
                                      kwargs={'cwd': str(ROOT)})
            thread.start()
            self.assertTrue(started.wait(5))
            entrypoint.shutdown()
            entrypoint.terminate_children()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(entrypoint._procs, {})

    def test_init_creates_full_bare_clone_and_retains_it_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, clone = root / 'source', root / 'managed.git'
            subprocess.run(['git', 'init', '-q', '-b', 'testing', str(source)], check=True)
            (source / 'test.spec').write_text('Version: 1\n')
            subprocess.run(['git', '-C', str(source), 'add', 'test.spec'], check=True)
            subprocess.run(['git', '-C', str(source), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                            'commit', '-q', '-m', 'Initial fixture'], check=True)
            env = {**os.environ, 'SPEC_REPO_URL': str(source), 'SPEC_REPO_DIR': str(clone),
                   'SPEC_REPO_BRANCH': 'testing'}
            command = ['sh', str(ROOT / 'deploy/init-spec-repo.sh')]
            first = subprocess.run(command, env=env, text=True, capture_output=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(subprocess.check_output(['git', '-C', str(clone), 'rev-list', '--count', 'HEAD'], text=True).strip(), '1')
            self.assertFalse((clone / 'shallow').exists())
            source.rename(root / 'offline')
            second = subprocess.run(command, env=env, text=True, capture_output=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn('retaining the existing SPEC clone', second.stderr)
            self.assertIn('SPEC clone ready:', second.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
