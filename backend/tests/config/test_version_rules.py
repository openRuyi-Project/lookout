import hashlib

import pytest

from tracker.monitors.version import rules


def test_native_file_is_the_only_rule_owner(tmp_path):
    path = tmp_path / 'nvchecker.toml'
    text = '[__config__]\nhttp_timeout=20\n[widget]\nsource="pypi"\npypi="widget"\n'
    path.write_text(text)
    (tmp_path / 'widget.toml').write_text('source="pypi"\npypi="wrong"\n')
    (tmp_path / 'unrelated.toml').write_text('[widget]\ntrack_label="unconfigured"\n')
    loaded = rules.load(path)
    assert loaded.entries == {'widget': {'source': 'pypi', 'pypi': 'widget'}}
    assert loaded.options == {'http_timeout': 20}
    assert loaded.digest == rules.digest(path) == hashlib.sha256(text.encode()).hexdigest()
    assert rules.files(path) == [path]


@pytest.mark.parametrize('text', [
    'widget=1\n',
    '[widget]\npypi="widget"\n', '[widget]\nsource=""\n',
    '[__unexpected__]\nsource="pypi"\n', '__config__=1\n',
    '[widget]\nsource="pypi"\n[widget]\nsource="git"\n',
])
def test_non_native_or_ambiguous_configuration_rejected(tmp_path, text):
    path = tmp_path / 'nvchecker.toml'
    path.write_text(text)
    with pytest.raises(ValueError):
        rules.load(path)


def test_digest_tracks_parsed_bytes_and_rejects_symlinks(tmp_path):
    path = tmp_path / 'native.toml'
    path.write_text('[widget]\nsource="pypi"\npypi="widget"\n')
    before = rules.load(path)
    path.write_text(path.read_text() + '# new observation policy\n')
    assert rules.digest(path) != before.digest
    link = tmp_path / 'link.toml'
    link.symlink_to(path)
    with pytest.raises(ValueError, match='regular file'):
        rules.load(link)


@pytest.mark.parametrize('size', [0, 1, 262145, 1048576])
def test_file_digests_preserve_exact_bytes(tmp_path, size):
    import hashlib

    from tracker import config_change
    from tracker.monitors.version import rules

    path = tmp_path / 'rules.toml'
    content = (b'\x00\xff\r\n[rule]\n' * (size // 11 + 1))[:size]
    path.write_bytes(content)
    expected = hashlib.sha256(content).hexdigest()
    assert config_change.digest(path) == rules.digest(path) == expected
    assert path.read_bytes() == content
