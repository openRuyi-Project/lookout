"""Heartbeat discovery never needs registry requests for unchanged publications."""
import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from tests.deployment.test_upgrade import module, OLD, NEW
import publication
import automation_tools


REVISION = 'a' * 40
NEXT = 'b' * 40
REFERENCE = 'ghcr.io/example/lookout:main'


def result(revision=REVISION):
    return {'workflow_runs': [dict(status='completed', conclusion='success', head_branch='main',
                                  event='push', head_sha=revision, head_repository={'full_name': 'example/lookout'})]}


@pytest.mark.parametrize('revision, expected', [(REVISION, OLD), (NEXT, 'ghcr.io/example/lookout:sha-' + NEXT)])
def test_successful_publication_pins_revision_without_registry(revision, expected, monkeypatch):
    requests = []
    def open(request, timeout):
        requests.append(request)
        assert timeout == 20
        return io.BytesIO(json.dumps(result(revision)).encode())
    monkeypatch.setattr(publication, 'urlopen', open)
    assert publication.published_image(REFERENCE, OLD, REVISION, 'checks.yml') == (expected, revision)
    assert len(requests) == 1
    assert requests[0].full_url.startswith('https://api.github.com/repos/example/lookout/actions/workflows/checks.yml/runs?')
    assert 'status=success' in requests[0].full_url


@pytest.mark.parametrize('field, value', [('conclusion', 'failure'), ('status', 'in_progress'),
                                         ('head_branch', 'topic'), ('event', 'pull_request'),
                                         ('head_sha', 'bad'), ('head_repository', {'full_name': 'other/lookout'})])
def test_ineligible_publication_is_rejected(field, value, monkeypatch):
    data = result()
    data['workflow_runs'][0][field] = value
    monkeypatch.setattr(publication, 'urlopen', lambda *a, **k: io.BytesIO(json.dumps(data).encode()))
    with pytest.raises(ValueError, match='successful channel'):
        publication.published_image(REFERENCE, OLD, REVISION, 'checks.yml')


def test_rate_limit_does_not_fall_back_to_registry(monkeypatch):
    def limited(*args, **kwargs):
        raise HTTPError('https://api.github.com', 403, 'rate limit', {}, None)
    monkeypatch.setattr(publication, 'urlopen', limited)
    with pytest.raises(RuntimeError, match='service unchanged'):
        publication.published_image(REFERENCE, OLD, REVISION, 'checks.yml')


@pytest.mark.parametrize('body', [b'{', b'{}', b'{"workflow_runs":null}', b'x' * (publication._MAX_RESPONSE + 1)])
def test_invalid_or_oversized_response_fails_closed(body, monkeypatch):
    monkeypatch.setattr(publication, 'urlopen', lambda *a, **k: io.BytesIO(body))
    with pytest.raises((ValueError, KeyError)):
        publication.published_image(REFERENCE, OLD, REVISION, 'checks.yml')


def test_empty_publication_list_keeps_current_image(monkeypatch):
    monkeypatch.setattr(publication, 'urlopen', lambda *a, **k: io.BytesIO(b'{"workflow_runs":[]}'))
    assert publication.published_image(REFERENCE, OLD, REVISION, 'checks.yml') == (OLD, REVISION)


def test_unchanged_heartbeat_has_no_registry_calls_or_probe_containers(tmp_path, monkeypatch):
    launcher = module('upgrade')
    calls = []
    monkeypatch.setattr(launcher, 'Docker', lambda _: SimpleNamespace(engine='docker', name='fixture', settings={'data': '/data'}))
    def run(argv):
        calls.append(argv)
        if argv[1] == 'inspect':
            return json.dumps([{'Image': OLD}])
        assert argv == ['docker', 'image', 'inspect', OLD]
        return json.dumps([{'Config': {'Labels': {'org.opencontainers.image.revision': REVISION}}}])
    monkeypatch.setattr(launcher, 'run', run)
    monkeypatch.setattr(launcher, 'published_image', lambda *args: (OLD, REVISION))
    monkeypatch.setattr(launcher, 'resolve_image', lambda *a: pytest.fail('must not pull or probe'))
    monkeypatch.setattr(launcher, 'healthy', lambda *a: None)
    for _ in range(3):
        observed = launcher.upgrade(REFERENCE, None, tmp_path, container='fixture', apply=True, workflow='checks.yml')
        assert observed['status'] == 'unchanged'
    assert not any('pull' in call or 'manifest' in call or 'run' in call for call in calls)


def test_tool_pointer_switches_complete_directories_and_retains_previous(tmp_path, monkeypatch):
    calls = []
    def export(engine, image, source, destination):
        calls.append(image)
        destination.mkdir()
        for name in ('upgrade.py', 'release-upgrade.py', 'deployment.py', 'publication.py', 'automation_tools.py', 'maintain.py'):
            (destination / name).write_text(image)
    monkeypatch.setattr(automation_tools, 'export_image_tree', export)
    pointer = tmp_path / 'current'
    automation_tools.refresh_tools('docker', OLD, pointer)
    previous = pointer.resolve()
    automation_tools.stage_tools('docker', NEW, pointer)
    assert pointer.resolve() == previous
    automation_tools.refresh_tools('docker', NEW, pointer)
    assert pointer.resolve() != previous
    assert (previous / 'upgrade.py').read_text() == OLD
    assert (pointer / 'upgrade.py').read_text() == NEW
    assert calls == [OLD, NEW]


def test_missing_protocol_leaves_old_tool_pointer(tmp_path, monkeypatch):
    previous = tmp_path / 'previous'
    previous.mkdir()
    pointer = tmp_path / 'current'
    pointer.symlink_to('previous')
    monkeypatch.setattr(automation_tools, 'export_image_tree', lambda engine, image, source, destination: destination.mkdir())
    with pytest.raises(ValueError, match='missing the host automation protocol'):
        automation_tools.refresh_tools('docker', NEW, pointer)
    assert pointer.resolve() == previous
    assert not (tmp_path / NEW.removeprefix('sha256:')).exists()
    assert not list(tmp_path.glob('.tools-*'))


def test_regular_tool_directory_is_not_overwritten(tmp_path):
    with pytest.raises(ValueError, match='pointer'):
        automation_tools.refresh_tools('docker', OLD, tmp_path)


def test_cli_defaults_ghcr_channels_to_publication_discovery(tmp_path, monkeypatch):
    launcher = module('upgrade')
    captured = []
    monkeypatch.setattr(launcher, 'upgrade', lambda *a, **k: captured.append(k) or {'status': 'unchanged'})
    assert launcher.main(['--image', REFERENCE, '--container', 'fixture', '--backups', str(tmp_path), '--apply', '--quiet']) == 0
    assert captured[0]['workflow'] == 'checks.yml'


@pytest.mark.parametrize('cached', [False, True])
@pytest.mark.parametrize('worker_status', [0, 2])
def test_new_publication_reuses_cache_and_updates_tools_only_after_success(tmp_path, monkeypatch, cached, worker_status):
    launcher = module('upgrade')
    events = []
    pinned = 'ghcr.io/example/lookout:sha-' + NEXT
    tools = tmp_path / 'tools'
    tools.mkdir()
    (tools / 'release-upgrade.py').write_text('# fixture')
    monkeypatch.setattr(launcher, 'Docker', lambda _: SimpleNamespace(engine='docker', name='fixture', settings={'data': '/data'}))
    def run(argv):
        if argv[1] == 'inspect':
            return json.dumps([{'Image': OLD}])
        if argv[-1] == OLD:
            return json.dumps([{'Config': {'Labels': {'org.opencontainers.image.revision': REVISION}}}])
        assert argv[-1] == pinned
        if not cached:
            raise RuntimeError('image absent')
        return json.dumps([{'Id': NEW, 'Config': {'Labels': {'org.opencontainers.image.revision': NEXT}}}])
    monkeypatch.setattr(launcher, 'run', run)
    monkeypatch.setattr(launcher, 'published_image', lambda *args: (pinned, NEXT))
    def resolve(engine, selected):
        events.append(('resolve', selected))
        return dict(image=NEW, revision=NEXT)
    monkeypatch.setattr(launcher, 'resolve_image', resolve)
    monkeypatch.setattr(launcher, 'stage_tools', lambda *a: tools)
    def worker(*args, **kwargs):
        events.append(('worker', NEW))
        return SimpleNamespace(returncode=worker_status, stderr='fixture failed' if worker_status else '',
                               stdout=json.dumps({'status': 'ready', 'image': NEW}))
    monkeypatch.setattr(launcher.subprocess, 'run', worker)
    monkeypatch.setattr(launcher, 'refresh_tools', lambda *a: events.append(('tools', NEW)))
    call = lambda: launcher.upgrade(REFERENCE, None, tmp_path, container='fixture', apply=True,
                                    workflow='checks.yml', tools_link=tmp_path / 'current')
    if worker_status:
        with pytest.raises(RuntimeError, match='fixture failed'):
            call()
        assert not any(event[0] == 'tools' for event in events)
    else:
        assert call()['status'] == 'ready'
        assert events[-2:] == [('worker', NEW), ('tools', NEW)]
    assert events[0] == ('resolve', NEW if cached else pinned)


def test_publication_failure_cannot_trigger_registry_fallback(tmp_path, monkeypatch):
    launcher = module('upgrade')
    monkeypatch.setattr(launcher, 'Docker', lambda _: SimpleNamespace(engine='docker', name='fixture', settings={'data': '/data'}))
    def run(argv):
        if argv[1] == 'inspect':
            return json.dumps([{'Image': OLD}])
        return json.dumps([{'Config': {'Labels': {'org.opencontainers.image.revision': REVISION}}}])
    monkeypatch.setattr(launcher, 'run', run)
    def unavailable(*args):
        raise RuntimeError('GitHub unavailable')
    monkeypatch.setattr(launcher, 'published_image', unavailable)
    monkeypatch.setattr(launcher, 'resolve_image', lambda *a: pytest.fail('registry fallback'))
    with pytest.raises(RuntimeError, match='GitHub unavailable'):
        launcher.upgrade(REFERENCE, None, tmp_path, container='fixture', apply=True, workflow='checks.yml')


def test_unchanged_tool_pointer_does_not_republish(tmp_path, monkeypatch):
    destination = tmp_path / OLD.removeprefix('sha256:')
    destination.mkdir()
    for name in ('upgrade.py', 'release-upgrade.py', 'deployment.py', 'publication.py', 'automation_tools.py', 'maintain.py'):
        (destination / name).write_text('# fixture')
    pointer = tmp_path / 'current'
    pointer.symlink_to(destination.name)
    before = pointer.lstat()
    monkeypatch.setattr(automation_tools, 'export_image_tree', lambda *a: pytest.fail('must reuse local tools'))
    monkeypatch.setattr(automation_tools.os, 'replace', lambda *a: pytest.fail('must not republish unchanged pointer'))
    assert automation_tools.refresh_tools('docker', OLD, pointer) == pointer
    assert pointer.lstat() == before


def test_incomplete_export_can_retry_same_image(tmp_path, monkeypatch):
    pointer = tmp_path / 'current'
    attempts = []
    def export(engine, image, source, destination):
        attempts.append(image)
        destination.mkdir()
        names = ('upgrade.py', 'release-upgrade.py', 'deployment.py', 'publication.py', 'automation_tools.py', 'maintain.py')
        for name in names[:1] if len(attempts) == 1 else names:
            (destination / name).write_text('# fixture')
    monkeypatch.setattr(automation_tools, 'export_image_tree', export)
    with pytest.raises(ValueError, match='missing the host automation protocol'):
        automation_tools.refresh_tools('docker', NEW, pointer)
    assert not pointer.exists()
    automation_tools.refresh_tools('docker', NEW, pointer)
    assert pointer.resolve().name == NEW.removeprefix('sha256:')
    assert attempts == [NEW, NEW]


def test_tool_pointer_accepts_a_symlinked_parent_directory(tmp_path, monkeypatch):
    directory = tmp_path / 'tools'
    directory.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(directory, target_is_directory=True)
    previous = directory / 'previous'
    previous.mkdir()
    pointer = alias / 'current'
    pointer.symlink_to('previous')
    def export(engine, image, source, destination):
        destination.mkdir()
        for name in ('upgrade.py', 'release-upgrade.py', 'deployment.py', 'publication.py', 'automation_tools.py', 'maintain.py'):
            (destination / name).write_text('# fixture')
    monkeypatch.setattr(automation_tools, 'export_image_tree', export)
    automation_tools.refresh_tools('docker', NEW, pointer)
    assert pointer.resolve() == directory / NEW.removeprefix('sha256:')
    assert previous.is_dir()
