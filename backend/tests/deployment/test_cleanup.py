import json
from types import SimpleNamespace
from tests.deployment.test_upgrade import module


def test_cleanup_protects_current_rollback_other_containers_and_unrelated_images(tmp_path, monkeypatch):
    tool = module('cleanup')
    images = ['sha256:' + str(n) * 64 for n in range(1, 7)]
    for n in range(3):
        directory = tmp_path / ('upgrade-' + str(n))
        directory.mkdir()
        (directory / 'image.json').write_text(json.dumps({'container': 'fixture', 'engine': 'podman',
            'image': images[n + 1], 'previous_image': images[n]}))
        (directory / 'result.json').write_text('{"status":"ready"}')
        import os
        os.utime(directory / 'result.json', ns=(n + 1, n + 1))
    def run(argv):
        if argv[1] == 'images':
            return '\n'.join(images)
        if argv[1] == 'ps':
            return 'other'
        return json.dumps([{'Image': images[3] if argv[-1] == 'fixture' else images[0]}])
    monkeypatch.setattr(tool, 'run', run)
    service = SimpleNamespace(engine='podman', name='fixture')
    assert tool.candidates(service, tmp_path) == [images[1]]
