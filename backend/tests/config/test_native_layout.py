"""Native TOML spelling must not change rules, their locations or promotion."""
import json
import subprocess
import tomllib

import pytest
from nvchecker.core import load_file

from tests.helpers.config import setup_config
from tracker import config, config_change, package
from tracker.monitors.version import rules

COMPACT = r'''# Source identity, independent of the downstream name.
"alpha.with.dot" = {source = "pypi", pypi = "upstream-alpha"}
# Keep this track's release policy beside it.
"beta@stable" = {source = "git", git = "https://fixture.example/beta", include_regex = '^v2\.[0-9]+$'}

[__config__]
http_timeout = 20
'''


def test_explain_locates_root_inline_rules_and_ordinary_tables(tmp_path):
    path = setup_config(tmp_path / 'config')
    native = path.parent / 'native.toml'
    native.write_text(COMPACT)
    for name, line in [('alpha.with.dot', 2), ('beta@stable', 4)]:
        result = package.explain(path, name)
        assert result['rules'][0]['line'] == line
        assert result['rules'][0]['table'] == [name]
        assert result['rules'][0]['fingerprint'] == config.track_fingerprint(tomllib.loads(COMPACT)[name])
    assert package.location(native, ('__config__',))['line'] == 6


def test_inline_edit_keeps_other_rules_and_comments_byte_identical():
    entry = tomllib.loads(COMPACT)['alpha.with.dot']
    entry['pypi'] = 'corrected-alpha'
    result = config_change.edit_tables(COMPACT, {'alpha.with.dot': entry})
    assert result == COMPACT.replace('upstream-alpha', 'corrected-alpha')
    assert config_change.edit_tables(result, {'alpha.with.dot': entry}) == result


def test_inline_promotion_roundtrip_preserves_operator_options(tmp_path):
    paths = [setup_config(tmp_path / name) for name in ('base', 'candidate', 'runtime')]
    for path in paths:
        (path.parent / 'native.toml').write_text(COMPACT)
    base, candidate, runtime = paths
    (candidate.parent / 'native.toml').write_text(COMPACT.replace('upstream-alpha', 'corrected-alpha'))
    operator = COMPACT.replace('http_timeout = 20', 'http_timeout = 30')
    (runtime.parent / 'native.toml').write_text(operator)
    review, prepared = tmp_path / 'review', tmp_path / 'prepared'
    result = config_change.plan(base, candidate, runtime, review)
    assert result['changed_tracks'] == ['alpha.with.dot']
    config_change.apply(review, runtime, prepared)
    assert (prepared / 'native.toml').read_text() == operator.replace('upstream-alpha', 'corrected-alpha')
    assert config.load(prepared / 'tracker.toml')['native_options']['http_timeout'] == 30


def test_duplicate_inline_and_block_rule_is_rejected(tmp_path):
    path = tmp_path / 'native.toml'
    path.write_text(COMPACT + '\n["alpha.with.dot"]\nsource="pypi"\npypi="other"\n')
    with pytest.raises(ValueError):
        rules.load(path)


@pytest.mark.parametrize('inline', [False, True])
def test_native_checker_accepts_both_spellings_without_expansion(tmp_path, inline):
    entry = {'source': 'cmd', 'cmd': "printf 'v2.7\\n'", 'prefix': 'v'}
    fields = [f'{key} = {json.dumps(value)}' for key, value in entry.items()]
    text = 'fixture = {' + ', '.join(fields) + '}\n' if inline else '[fixture]\n' + '\n'.join(fields) + '\n'
    path = tmp_path / 'native.toml'
    path.write_text(text)
    native, _ = load_file(str(path), use_keymanager=False)
    assert rules.same_values(native, {'fixture': entry})
    result = subprocess.run(['nvchecker', '--logger=json', '--json-log-fd=1', '--failures', '-c', str(path)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr + result.stdout
    versions = [json.loads(line)['version'] for line in result.stdout.splitlines()
                if 'version' in json.loads(line)]
    assert versions == ['2.7']
    assert path.read_text() == text
