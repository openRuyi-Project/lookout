import pytest

from tracker import config_change, config
from test_architecture_repairs import setup_config

NATIVE = '''# Native, independent package rules.
[__config__]
http_timeout = 20

[alpha]
source = "pypi"
pypi = "special" # upstream identity, not the RPM prefix

# Independent maintenance line; keep its explanation.
[gamma]
source = "git"
git = "https://example.org/gamma"
include_regex = '^v3[.].*'
'''


def configs(tmp_path):
    paths = []
    for name in ['base', 'candidate', 'runtime']:
        path = setup_config(tmp_path / name)
        path.write_text(path.read_text().replace('native.toml', 'versions/nvchecker.toml'))
        (path.parent / 'native.toml').unlink()
        (path.parent / 'versions').mkdir()
        (path.parent / 'versions/nvchecker.toml').write_text(NATIVE)
        paths.append(path)
    return paths


def test_unchanged_plan_preserves_all_authored_text(tmp_path):
    base, candidate, runtime = configs(tmp_path)
    review = tmp_path / 'review'
    config_change.plan(base, candidate, runtime, review)
    for path in runtime.parent.rglob('*.toml'):
        assert (review / path.relative_to(runtime.parent)).read_bytes() == path.read_bytes()


def test_single_rule_edit_preserves_unrelated_tables_and_comments(tmp_path):
    base, candidate, runtime = configs(tmp_path)
    path = candidate.parent / 'versions/nvchecker.toml'
    path.write_text(NATIVE.replace('"special"', '"corrected"'))
    operator = runtime.parent / 'versions/nvchecker.toml'
    operator.write_text(NATIVE.replace('http_timeout = 20', 'http_timeout = 30').replace(
        'Independent maintenance', 'Operator-owned maintenance'))
    review = tmp_path / 'review'
    result = config_change.plan(base, candidate, runtime, review)
    assert result['changed_tracks'] == ['alpha']
    expected = operator.read_text().replace('"special"', '"corrected"')
    assert (review / 'versions/nvchecker.toml').read_text() == expected
    config_change.apply(review, runtime, tmp_path / 'prepared')
    assert (tmp_path / 'prepared/versions/nvchecker.toml').read_text() == expected
    assert config.load(tmp_path / 'prepared/tracker.toml')['native_options']['http_timeout'] == 30


def test_overlapping_literal_edit_is_not_silently_reformatted():
    with pytest.raises(ValueError, match='text changes conflict'):
        config_change.merge_text('a\nb\nc\n', 'a\nx\nc\n', 'a\ny\nc\n', 'nvchecker.toml')
