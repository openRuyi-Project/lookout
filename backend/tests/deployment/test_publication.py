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
    previous = tmp_path / 'bootstrap'
    previous.mkdir()
    pointer = tmp_path / 'current'
    pointer.symlink_to(previous.name)
    monkeypatch.setattr(launcher, 'Docker', lambda _: SimpleNamespace(engine='docker', name='fixture', settings={'data': '/data'}))
    def run(argv):
        calls.append(argv)
        if argv[1] == 'inspect':
            return json.dumps([{'Image': OLD}])
        assert argv == ['docker', 'image', 'inspect', OLD]
        return json.dumps([{'Config': {'Labels': {'org.opencontainers.image.revision': REVISION}}}])
    monkeypatch.setattr(launcher, 'run', run)
    monkeypatch.setattr(launcher, 'published_image', lambda *args, **kwargs: (OLD, REVISION))
    monkeypatch.setattr(launcher, 'resolve_image', lambda *a: pytest.fail('must not pull or probe'))
    monkeypatch.setattr(launcher, 'healthy', lambda *a: None)
    for _ in range(3):
        observed = launcher.upgrade(REFERENCE, None, tmp_path, container='fixture', apply=True, workflow='checks.yml', tools_link=pointer)
        assert observed['status'] == 'unchanged'
        assert pointer.resolve() == previous
    assert not any('pull' in call or 'manifest' in call or 'run' in call for call in calls)


def test_tool_pointer_switches_complete_directories_and_retains_previous(tmp_path, monkeypatch):
    calls = []
    def export(engine, image, source, destination):
        calls.append(image)
        destination.mkdir()
        for name in ('upgrade.py', 'release-upgrade.py', 'deployment.py', 'publication.py', 'credentials.py', 'automation_tools.py', 'maintain.py'):
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
    monkeypatch.setattr(launcher, 'published_image', lambda *args, **kwargs: (pinned, NEXT))
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
    def unavailable(*args, **kwargs):
        raise RuntimeError('GitHub unavailable')
    monkeypatch.setattr(launcher, 'published_image', unavailable)
    monkeypatch.setattr(launcher, 'resolve_image', lambda *a: pytest.fail('registry fallback'))
    with pytest.raises(RuntimeError, match='GitHub unavailable'):
        launcher.upgrade(REFERENCE, None, tmp_path, container='fixture', apply=True, workflow='checks.yml')


def test_unchanged_tool_pointer_does_not_republish(tmp_path, monkeypatch):
    destination = tmp_path / OLD.removeprefix('sha256:')
    destination.mkdir()
    for name in ('upgrade.py', 'release-upgrade.py', 'deployment.py', 'publication.py', 'credentials.py', 'automation_tools.py', 'maintain.py'):
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
        names = ('upgrade.py', 'release-upgrade.py', 'deployment.py', 'publication.py', 'credentials.py', 'automation_tools.py', 'maintain.py')
        for name in names[:1] if len(attempts) == 1 else names:
            (destination / name).write_text('# fixture')
    monkeypatch.setattr(automation_tools, 'export_image_tree', export)
    with pytest.raises(ValueError, match='missing the host automation protocol'):
        automation_tools.refresh_tools('docker', NEW, pointer)
    assert not pointer.exists()
    automation_tools.refresh_tools('docker', NEW, pointer)
    assert pointer.resolve().name == NEW.removeprefix('sha256:')
    assert attempts == [NEW, NEW]


@pytest.mark.parametrize('workflow', [None, 'checks.yml'])
@pytest.mark.parametrize('healthy', [False, True])
def test_unchanged_release_recovers_staged_tools_without_redeploy(tmp_path, monkeypatch, workflow, healthy):
    launcher = module('upgrade')
    previous = tmp_path / 'previous'
    previous.mkdir()
    pointer = tmp_path / 'current'
    pointer.symlink_to(previous.name)
    staged = tmp_path / OLD.removeprefix('sha256:')
    staged.mkdir()
    for name in ('upgrade.py', 'release-upgrade.py', 'deployment.py', 'publication.py', 'credentials.py', 'automation_tools.py', 'maintain.py'):
        (staged / name).write_text('# fixture')
    monkeypatch.setattr(launcher, 'Docker', lambda _: SimpleNamespace(engine='docker', name='fixture', settings={'data': '/data'}))
    def run(argv):
        if argv[1] == 'inspect':
            return json.dumps([{'Image': OLD}])
        assert argv == ['docker', 'image', 'inspect', OLD]
        return json.dumps([{'Config': {'Labels': {'org.opencontainers.image.revision': REVISION}}}])
    monkeypatch.setattr(launcher, 'run', run)
    monkeypatch.setattr(launcher, 'published_image', lambda *a, **kw: (OLD, REVISION))
    monkeypatch.setattr(launcher, 'unchanged_image', lambda *a: True)
    monkeypatch.setattr(launcher, 'resolve_image', lambda *a: {'image': OLD, 'revision': REVISION})
    def health(*args):
        if not healthy:
            raise RuntimeError('not ready')
    monkeypatch.setattr(launcher, 'healthy', health)
    monkeypatch.setattr(automation_tools, 'export_image_tree', lambda *a: pytest.fail('must reuse staged tools'))
    monkeypatch.setattr(launcher.subprocess, 'run', lambda *a, **k: pytest.fail('must not redeploy'))
    call = lambda: launcher.upgrade(REFERENCE, None, tmp_path, container='fixture', apply=True,
                                    workflow=workflow, tools_link=pointer)
    if not healthy:
        with pytest.raises(RuntimeError, match='not ready'):
            call()
        assert pointer.resolve() == previous
    else:
        assert call()['status'] == 'unchanged'
        assert pointer.resolve() == staged
        unchanged = pointer.lstat()
        assert call()['status'] == 'unchanged'
        assert pointer.lstat() == unchanged


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
        for name in ('upgrade.py', 'release-upgrade.py', 'deployment.py', 'publication.py', 'credentials.py', 'automation_tools.py', 'maintain.py'):
            (destination / name).write_text('# fixture')
    monkeypatch.setattr(automation_tools, 'export_image_tree', export)
    automation_tools.refresh_tools('docker', NEW, pointer)
    assert pointer.resolve() == directory / NEW.removeprefix('sha256:')
    assert previous.is_dir()


def test_limit_survives_process_restart_and_recovers(tmp_path, monkeypatch):
    path = tmp_path / 'publication.json'
    clock = [1000]
    monkeypatch.setattr(publication.time, 'time', lambda: clock[0])
    calls = []
    def open(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise HTTPError(request.full_url, 403, 'limited',
                            {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1600'}, None)
        return io.BytesIO(json.dumps(result(NEXT)).encode())
    monkeypatch.setattr(publication, 'urlopen', open)
    for clock[0] in [1000, 1100, 1599]:
        with pytest.raises(publication.Deferred) as error:
            publication.published_image(REFERENCE, OLD, REVISION, 'checks.yml', state_path=path)
        assert error.value.retry_at == 1600
    assert len(calls) == 1
    clock[0] = 1600
    assert publication.published_image(REFERENCE, OLD, REVISION, 'checks.yml', state_path=path)[1] == NEXT
    assert len(calls) == 2
    assert 'next_attempt_at' not in json.loads(path.read_text())


def test_authenticated_etag_and_no_secret_in_checkpoint(tmp_path, monkeypatch):
    credential = tmp_path / 'github.env'
    credential.write_text('LOOKOUT_GITHUB_TOKEN=fixture_secret\n')
    credential.chmod(0o600)
    checkpoint = tmp_path / 'publication.json'
    calls = []
    def open(request, timeout):
        calls.append(request)
        assert request.get_header('Authorization') == 'Bearer fixture_secret'
        if len(calls) == 2:
            assert request.get_header('If-none-match') == 'fixture-etag'
            raise HTTPError(request.full_url, 304, 'unchanged', {}, None)
        response = io.BytesIO(json.dumps(result()).encode())
        response.headers = {'ETag': 'fixture-etag'}
        return response
    monkeypatch.setattr(publication, 'urlopen', open)
    for _ in range(2):
        assert publication.published_image(REFERENCE, OLD, REVISION, 'checks.yml',
            state_path=checkpoint, credential_file=credential) == (OLD, REVISION)
    assert 'fixture_secret' not in checkpoint.read_text()


@pytest.mark.parametrize('mode,content', [(0o644, 'LOOKOUT_GITHUB_TOKEN=fixture'),
                                         (0o600, 'LOOKOUT_GITHUB_TOKEN=fixture\nHOST=bad'),
                                         (0o600, 'LOOKOUT_GITHUB_TOKEN=')])
def test_credential_rejects_exposure_and_extra_environment(tmp_path, mode, content):
    from credentials import github_token
    path = tmp_path / 'github.env'
    path.write_text(content)
    path.chmod(mode)
    with pytest.raises(ValueError):
        github_token(path)


def test_deferred_upgrade_never_inspects_registry(tmp_path, monkeypatch):
    launcher = module('upgrade')
    monkeypatch.setattr(launcher, 'Docker', lambda _: SimpleNamespace(engine='docker', name='fixture', settings={'data': '/data'}))
    def run(argv):
        if argv[1] == 'inspect':
            return json.dumps([{'Image': OLD}])
        assert argv == ['docker', 'image', 'inspect', OLD]
        return json.dumps([{'Config': {'Labels': {'org.opencontainers.image.revision': REVISION}}}])
    monkeypatch.setattr(launcher, 'run', run)
    def waiting(*args, **kwargs):
        raise publication.Deferred('GitHub rate limit', 2000)
    monkeypatch.setattr(launcher, 'published_image', waiting)
    monkeypatch.setattr(launcher, 'resolve_image', lambda *a: pytest.fail('must not pull'))
    observed = launcher.upgrade(REFERENCE, None, tmp_path, container='fixture', apply=True, workflow='checks.yml')
    assert observed['status'] == 'deferred' and observed['next_attempt_at'] == 2000


def test_credential_symlink_is_rejected(tmp_path):
    from credentials import github_token
    target = tmp_path / 'real'
    target.write_text('LOOKOUT_GITHUB_TOKEN=fixture')
    target.chmod(0o600)
    link = tmp_path / 'link'
    link.symlink_to(target)
    with pytest.raises(ValueError):
        github_token(link)


def test_rejected_token_uses_public_and_remembers_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv('LOOKOUT_GITHUB_TOKEN', 'fixture_only')
    calls = []
    def open(request, timeout):
        authenticated = bool(request.get_header('Authorization'))
        calls.append(authenticated)
        if authenticated:
            raise HTTPError(request.full_url, 401, 'Unauthorized', {}, io.BytesIO(b'{}'))
        return io.BytesIO(json.dumps(result()).encode())
    monkeypatch.setattr(publication, 'urlopen', open)
    path = tmp_path / 'checkpoint.json'
    for _ in range(2):
        assert publication.published_image(REFERENCE, OLD, REVISION, 'checks.yml', state_path=path) == (OLD, REVISION)
    assert calls == [True, False, False]
    assert 'fixture_only' not in path.read_text()


def test_authenticated_quota_never_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv('LOOKOUT_GITHUB_TOKEN', 'fixture_only')
    calls = []
    def open(request, timeout):
        calls.append(request)
        raise HTTPError(request.full_url, 403, 'Limited', {'Retry-After': '300'}, None)
    monkeypatch.setattr(publication, 'urlopen', open)
    with pytest.raises(publication.Deferred):
        publication.published_image(REFERENCE, OLD, REVISION, 'checks.yml', state_path=tmp_path / 'checkpoint.json')
    assert len(calls) == 1


@pytest.mark.parametrize('accepted', [False, True])
def test_auth_probe_does_not_reset_public_wait(tmp_path, monkeypatch, accepted):
    monkeypatch.setenv('LOOKOUT_GITHUB_TOKEN', 'fixture_only')
    monkeypatch.setattr(publication.time, 'time', lambda: 1000)
    path = tmp_path / 'checkpoint.json'
    original = {'key': REFERENCE + '/checks.yml', 'auth_retry_at': 900,
                'next_attempt_at': 8000, 'reason': 'GitHub rate limit'}
    path.write_text(json.dumps(original))
    calls = []
    def open(request, timeout):
        calls.append(bool(request.get_header('Authorization')))
        if not accepted:
            raise HTTPError(request.full_url, 401, 'Unauthorized', {}, io.BytesIO(b'{}'))
        return io.BytesIO(json.dumps(result()).encode())
    monkeypatch.setattr(publication, 'urlopen', open)
    if accepted:
        assert publication.published_image(REFERENCE, OLD, REVISION, 'checks.yml', state_path=path) == (OLD, REVISION)
        assert json.loads(path.read_text())['auth_retry_at'] == 0
    else:
        with pytest.raises(publication.Deferred):
            publication.published_image(REFERENCE, OLD, REVISION, 'checks.yml', state_path=path)
        saved = json.loads(path.read_text())
        assert saved['next_attempt_at'] == 8000
        assert saved['auth_retry_at'] == 4600
    assert calls == [True]


def test_public_quota_after_failed_auth_probe_retains_its_own_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv('LOOKOUT_GITHUB_TOKEN', 'fixture_only')
    monkeypatch.setattr(publication.time, 'time', lambda: 1000)
    path = tmp_path / 'checkpoint.json'
    path.write_text(json.dumps({'key': REFERENCE + '/checks.yml', 'auth_retry_at': 900}))
    def open(request, timeout):
        if request.get_header('Authorization'):
            raise HTTPError(request.full_url, 401, 'Unauthorized', {}, io.BytesIO(b'{}'))
        raise HTTPError(request.full_url, 403, 'Limited', {'X-RateLimit-Remaining': '0',
                                                       'X-RateLimit-Reset': '8000'}, None)
    monkeypatch.setattr(publication, 'urlopen', open)
    with pytest.raises(publication.Deferred):
        publication.published_image(REFERENCE, OLD, REVISION, 'checks.yml', state_path=path)
    saved = json.loads(path.read_text())
    assert saved['auth_retry_at'] == 4600
    assert saved['next_attempt_at'] == 8000
