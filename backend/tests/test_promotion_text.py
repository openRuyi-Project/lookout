import pytest
import tomllib
import tomlkit
from hypothesis import given, strategies as st

from tracker import config_change, config, version_rules
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


POLICIES = '''# Operator-owned package policies.
[packages.alpha]
track_label = "old" # policy, not upstream identity
watch = ["alpha@prerelease"]

# gamma is independently maintained; this comment must stay here.
[packages.gamma]
track_label = '3.x'
'''


def test_binding_edit_preserves_adjacent_comments_and_unchanged_fields():
    changed = config_change.edit_tables(POLICIES, {
        'alpha': {'track_label': 'new', 'watch': ['alpha@prerelease']},
    }, ('packages',))
    assert changed == POLICIES.replace('track_label = "old"', 'track_label = "new"')


def test_semantic_noop_is_byte_identical_even_with_explicit_changes():
    policies = tomllib.loads(POLICIES)['packages']
    assert config_change.edit_tables(POLICIES, policies, ('packages',)) == POLICIES


@pytest.mark.parametrize('layout', [
    '''[packages."python.foo"]
track_label = "old"
monitors = {eol = {product = 'foo', cycle_parts = 2}}
''',
    '''[packages."python.foo"]
track_label = "old"
[packages."python.foo".monitors.eol]
product = 'foo'
cycle_parts = 2
''',
    '''[packages]
"python.foo" = {track_label = "old", monitors = {eol = {product = 'foo', cycle_parts = 2}}}
''',
])
def test_binding_edit_keeps_nested_and_inline_monitor_configuration(layout):
    policies = tomllib.loads(layout)['packages']
    policies['python.foo']['track_label'] = 'new'
    changed = config_change.edit_tables(layout, policies, ('packages',))
    assert changed == layout.replace('track_label = "old"', 'track_label = "new"')
    assert tomllib.loads(changed)['packages'] == policies


def test_unmodified_multiline_strings_and_nested_comments_are_retained():
    text = '''[packages.alpha]
track_label = "old"
[packages.alpha.monitors.fixture]
note = """keep the literal lines
[not_a_real_header]
and the rest"""
# monitor-specific explanation
enabled = true
'''
    policies = tomllib.loads(text)['packages']
    policies['alpha']['track_label'] = 'new'
    assert config_change.edit_tables(text, policies, ('packages',)) == text.replace('"old"', '"new"')


@pytest.mark.parametrize('text, expected', [
    ('[packages.alpha]\ntrack_label="old"\n', ''),
    ('[packages]\nalpha = {track_label="old"}\n', '[packages]\n'),
])
def test_removing_last_binding_preserves_explicit_parent_only(text, expected):
    assert config_change.edit_tables(text, {'alpha': None}, ('packages',)) == expected


def test_deleting_table_with_ambiguous_comment_ownership_fails_closed():
    with pytest.raises(ValueError, match='discard standalone comments'):
        config_change.edit_tables(POLICIES, {'alpha': None}, ('packages',))


def test_deleting_non_contiguous_table_does_not_drop_nested_comments():
    text = '''[packages.alpha.monitors.eol]
product="alpha"
# alpha tail
[packages.beta]
track_label="b"
[packages.alpha]
track_label="a"
'''
    with pytest.raises(ValueError, match='non-contiguous table'):
        config_change.edit_tables(text, {'alpha': None}, ('packages',))


@pytest.mark.parametrize('old,new', [
    (True, 1), (False, 0), (1, 1.0), (1.0, True),
    ([True, {'enabled': 1}], [1, {'enabled': 1.0}]),
    ({'enabled': True, 'items': [1]}, {'enabled': 1, 'items': [1.0]}),
])
def test_field_edit_preserves_requested_types_in_scalars_and_containers(old, new):
    fields = {'source': 'manual', 'value': old}
    document = tomlkit.document()
    table = tomlkit.table()
    for key, value in fields.items():
        if isinstance(value, dict):
            inline = tomlkit.inline_table()
            inline.update(value)
            value = inline
        table.add(key, value)
    document.add('widget', table)
    text = tomlkit.dumps(document)
    expected = {'widget': {**fields, 'value': new}}
    changed = config_change.edit_tables(text, expected)
    assert version_rules.same_values(tomllib.loads(changed), expected)
    assert changed != text


def test_typed_rebase_reports_changes_and_rejects_operator_type_conflicts():
    base = {'widget': {'enabled': True}}
    candidate = {'widget': {'enabled': 1}}
    assert config_change.rebase(base, candidate, base) == candidate
    assert config_change.rebase(base, candidate, candidate) == {}
    with pytest.raises(ValueError, match='operator changes conflict: widget'):
        config_change.rebase(base, candidate, {'widget': {'enabled': 1.0}})


def test_plan_apply_reports_and_retains_native_option_type_change(tmp_path):
    base, candidate, runtime = configs(tmp_path)
    for path in (base, candidate, runtime):
        native = path.parent / 'versions/nvchecker.toml'
        native.write_text(NATIVE.replace('[alpha]\n', '[alpha]\nuse_pre_release = true\n'))
    native = candidate.parent / 'versions/nvchecker.toml'
    native.write_text(native.read_text().replace('use_pre_release = true', 'use_pre_release = 1'))
    review = tmp_path / 'review'
    result = config_change.plan(base, candidate, runtime, review)
    assert result['changed_tracks'] == ['alpha']
    config_change.apply(review, runtime, tmp_path / 'prepared')
    actual = config.load(tmp_path / 'prepared/tracker.toml')['native']['alpha']['use_pre_release']
    assert type(actual) is int and actual == 1


def test_editor_semantic_guard_rejects_type_changing_serializer(monkeypatch):
    original = tomlkit.dumps
    monkeypatch.setattr(config_change.tomlkit, 'dumps', lambda document: original(document).replace(
        'value = 1', 'value = true'))
    with pytest.raises(ValueError, match='refusing lossy configuration edit'):
        config_change.edit_tables('[widget]\nvalue = 0\n', {'widget': {'value': 1}})


@given(st.dictionaries(
    st.text(alphabet=st.characters(blacklist_categories=('Cs', 'Cc')), min_size=1, max_size=12),
    st.one_of(st.booleans(), st.integers(), st.text(
        alphabet=st.characters(blacklist_categories=('Cs', 'Cc')), max_size=40)),
    max_size=8,
))
def test_arbitrary_field_edits_preserve_unrelated_policy_text(fields):
    suffix = POLICIES[POLICIES.index('# gamma'):]
    changed = config_change.edit_tables(POLICIES, {'alpha': fields}, ('packages',))
    parsed = tomllib.loads(changed)['packages']
    assert version_rules.same_values(parsed, {'alpha': fields, 'gamma': {'track_label': '3.x'}})
    assert changed.endswith(suffix)
    assert config_change.edit_tables(changed, {'alpha': fields}, ('packages',)) == changed
