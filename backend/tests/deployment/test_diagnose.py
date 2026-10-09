import json
from types import SimpleNamespace

from tests.deployment.test_upgrade import module


def test_diagnosis_redacts_environment_and_publication_body(tmp_path, monkeypatch):
    tool = module('diagnose')
    service = SimpleNamespace(engine='podman', name='fixture', settings={'github_env_file': 'private.env'})
    def run(args):
        if args[1] == 'inspect':
            return json.dumps([{'Image': 'sha256:fixture', 'State': {'Status': 'running'},
                'Config': {'Env': ['LOOKOUT_GITHUB_TOKEN=secret']}}])
        if args[1] == 'image':
            return json.dumps([{'Config': {'Labels': {'org.opencontainers.image.version': '1.0.0'}}}])
        return json.dumps({'live': {'http': 200}})
    monkeypatch.setattr(tool, 'run', run)
    monkeypatch.setattr(tool, 'status', lambda *args: {'ok': True})
    (tmp_path / 'publication.json').write_text(json.dumps({'body': 'secret', 'next_attempt_at': 2000, 'reason': 'limit'}))
    result = tool.diagnose(service, tmp_path)
    assert result['credential_configured']
    assert result['update'] == {'next_attempt_at': 2000, 'reason': 'limit'}
    assert 'secret' not in json.dumps(result)
