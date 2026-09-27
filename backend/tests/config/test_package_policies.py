"""Package policies have one explicit file and participate in every change guard."""
import hashlib
import os
from pathlib import Path
import tomllib

import pytest

from tests.helpers.config import setup_config
from tracker import config, config_change


def no_packages(path):
    path.write_text(path.read_text().replace('packages_config="packages.toml"\n', ''))
    (path.parent / 'packages.toml').unlink()


def test_package_policy_input_is_explicit_and_does_not_scan_siblings(tmp_path):
    path = setup_config(tmp_path / 'config')
    no_packages(path)
    (path.parent / 'packages.toml').write_text('[widget]\ntrack_label="ignored"\n')
    loaded = config.load(path)
    assert loaded['packages'] == {} and loaded['packages_path'] is None
    assert set(loaded['input_hashes']) == {str(path), str(path.parent / 'native.toml')}
    config.require_unchanged(loaded, path)


def test_loaded_file_map_covers_exactly_the_parsed_inputs(tmp_path):
    path = setup_config(tmp_path / 'config')
    policy = path.parent / 'packages.toml'
    policy.write_text('[widget]\ntrack_label="reviewed"\n[widget.monitors.eol]\nproduct="widget"\ncycle_parts=2\n')
    loaded = config.load(path)
    expected = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (path, policy, path.parent / 'native.toml')}
    assert loaded['input_hashes'] == expected
    assert loaded['config_digest'] == expected[str(path)]
    assert loaded['nv_digest'] == expected[loaded['nvpath']]
    assert loaded['packages_path'] == str(policy)
    assert loaded['packages'] == tomllib.loads(policy.read_text())


@pytest.mark.parametrize('reference', ['""', 'true', '[]', '"missing.toml"', '"tracker.toml"', '"native.toml"'])
def test_invalid_missing_or_overlapping_reference_fails(tmp_path, reference):
    path = setup_config(tmp_path / 'config')
    path.write_text(path.read_text().replace('"packages.toml"', reference))
    with pytest.raises(ValueError):
        config.load(path)


@pytest.mark.parametrize('source', ['tracker.toml', 'native.toml'])
def test_hardlinked_policy_cannot_alias_another_loaded_input(tmp_path, source):
    path = setup_config(tmp_path / 'config')
    policy = path.parent / 'packages.toml'
    policy.unlink()
    os.link(path.parent / source, policy)
    with pytest.raises(ValueError, match='distinct file'):
        config.load(path)


@pytest.mark.parametrize('reference', [True, False])
def test_inline_packages_are_never_a_fallback_or_override(tmp_path, reference):
    path = setup_config(tmp_path / 'config')
    if not reference:
        no_packages(path)
    path.write_text(path.read_text() + '\n[packages.widget]\ntrack_label="hidden"\n')
    with pytest.raises(ValueError, match='inline packages'):
        config.load(path)


@pytest.mark.parametrize('text', ['widget = "not a table"', '[packages.widget]\ntrack_label="nested"',
                                  '[widget]\nsource="pypi"', '[widget]\n[widget]\n'])
def test_package_file_rejects_wrong_shape_or_duplicate_authority(tmp_path, text):
    path = setup_config(tmp_path / 'config')
    (path.parent / 'packages.toml').write_text(text)
    with pytest.raises(ValueError):
        config.load(path)


@pytest.mark.parametrize('replacement', ['comment', 'missing', 'symlink', 'directory'])
def test_publication_guard_covers_package_input_content_and_kind(tmp_path, replacement):
    path = setup_config(tmp_path / 'config')
    loaded = config.load(path)
    policy = path.parent / 'packages.toml'
    if replacement == 'comment':
        policy.write_text('# changed after load\n')
    else:
        saved = policy.with_suffix('.saved')
        policy.rename(saved)
        if replacement == 'symlink':
            policy.symlink_to(saved)
        elif replacement == 'directory':
            policy.mkdir()
    with pytest.raises(ValueError, match='configuration changed'):
        config.require_unchanged(loaded)


POLICIES = '''# Widget's human-owned policy.
[widget]
track_label = "old" # not the upstream version
[widget.monitors.eol]
product = "widget"
cycle_parts = 2

# Independently reviewed package; leave it alone.
[other]
track_label = '7.x'
'''


def policy_configs(tmp_path):
    paths = [setup_config(tmp_path / name) for name in ('base', 'candidate', 'runtime')]
    for path in paths:
        (path.parent / 'packages.toml').write_text(POLICIES)
    return paths


def test_single_package_promotion_preserves_monitor_and_unrelated_text(tmp_path):
    base, candidate, runtime = policy_configs(tmp_path)
    (candidate.parent / 'packages.toml').write_text(POLICIES.replace('"old"', '"new"'))
    operator = POLICIES.replace('Independently reviewed', 'Operator-owned explanation for')
    (runtime.parent / 'packages.toml').write_text(operator)
    review, prepared = tmp_path / 'review', tmp_path / 'prepared'
    record = config_change.plan(base, candidate, runtime, review)
    assert record['changed_bindings'] == ['widget'] and record['changed_tracks'] == []
    assert set(record['baseline_hashes']) == {'tracker.toml', 'native.toml', 'packages.toml'}
    assert record['baseline_hashes'].keys() == record['proposed_hashes'].keys()
    config_change.apply(review, runtime, prepared)
    assert (prepared / 'packages.toml').read_text() == operator.replace('"old"', '"new"')
    loaded = config.load(prepared / 'tracker.toml')
    assert loaded['packages']['widget']['monitors'] == {'eol': {'product': 'widget', 'cycle_parts': 2}}
    assert loaded['packages']['widget']['track_label'] == 'new'
    assert (prepared / 'native.toml').read_bytes() == (runtime.parent / 'native.toml').read_bytes()


def test_noop_policy_promotion_is_byte_identical(tmp_path):
    paths = policy_configs(tmp_path)
    review = tmp_path / 'review'
    config_change.plan(*paths, review)
    for name in ('tracker.toml', 'packages.toml', 'native.toml'):
        assert (review / name).read_bytes() == (paths[2].parent / name).read_bytes()


def test_policy_conflict_requires_review(tmp_path):
    base, candidate, runtime = policy_configs(tmp_path)
    (candidate.parent / 'packages.toml').write_text(POLICIES.replace('"old"', '"candidate"'))
    (runtime.parent / 'packages.toml').write_text(POLICIES.replace('"old"', '"operator"'))
    with pytest.raises(ValueError, match='operator changes conflict: widget'):
        config_change.plan(base, candidate, runtime, tmp_path / 'review')


@pytest.mark.parametrize('where', ['runtime', 'review'])
def test_apply_rejects_package_policy_drift(tmp_path, where):
    paths = policy_configs(tmp_path)
    review = tmp_path / 'review'
    config_change.plan(*paths, review)
    policy = (paths[2].parent if where == 'runtime' else review) / 'packages.toml'
    policy.write_text(policy.read_text() + '# changed after review\n')
    with pytest.raises(ValueError, match='drift|reviewed candidate changed'):
        config_change.apply(review, paths[2], tmp_path / 'prepared')
    assert not (tmp_path / 'prepared').exists()


@pytest.mark.parametrize('after_copy', [False, True])
def test_apply_rechecks_review_and_copied_inputs_before_publication(tmp_path, monkeypatch, after_copy):
    paths = policy_configs(tmp_path)
    review, prepared = tmp_path / 'review', tmp_path / 'prepared'
    config_change.plan(*paths, review)
    original = config_change.shutil.copyfile

    def raced(source, destination, **kwargs):
        if Path(source) != review / 'packages.toml':
            return original(source, destination, **kwargs)
        if after_copy:
            result = original(source, destination, **kwargs)
        Path(source).write_text(Path(source).read_text().replace('"old"', '"raced"'))
        return result if after_copy else original(source, destination, **kwargs)

    monkeypatch.setattr(config_change.shutil, 'copyfile', raced)
    with pytest.raises(ValueError, match='differs from reviewed|configuration changed'):
        config_change.apply(review, paths[2], prepared)
    assert not prepared.exists()
    assert (paths[2].parent / 'packages.toml').read_text() == POLICIES


@pytest.mark.parametrize('reference', ['packages.toml', './packages.toml', 'policies//packages.toml'])
def test_first_package_override_is_an_explicit_reviewed_new_input(tmp_path, reference):
    base, candidate, runtime = [setup_config(tmp_path / name) for name in ('base', 'candidate', 'runtime')]
    no_packages(base)
    no_packages(runtime)
    (candidate.parent / 'packages.toml').unlink()
    package_file = candidate.parent / reference
    package_file.parent.mkdir(parents=True, exist_ok=True)
    package_file.write_text(POLICIES)
    candidate.write_text(candidate.read_text().replace('"packages.toml"', '"' + reference + '"'))
    review, prepared = tmp_path / 'review', tmp_path / 'prepared'
    record = config_change.plan(base, candidate, runtime, review)
    assert set(record['proposed_hashes']) - set(record['baseline_hashes']) == {str(Path(reference))}
    config_change.apply(review, runtime, prepared)
    loaded = config.load(prepared / 'tracker.toml')
    assert loaded['packages'] == tomllib.loads(POLICIES)
    assert loaded['packages_config'] == str(Path(reference))


def test_first_policy_file_does_not_overwrite_an_operator_file(tmp_path):
    base, candidate, runtime = [setup_config(tmp_path / name) for name in ('base', 'candidate', 'runtime')]
    no_packages(base)
    no_packages(runtime)
    (candidate.parent / 'packages.toml').write_text(POLICIES)
    existing = runtime.parent / 'packages.toml'
    existing.write_text('operator-owned unrelated bytes')
    with pytest.raises(ValueError, match='already exists'):
        config_change.plan(base, candidate, runtime, tmp_path / 'review')
    assert existing.read_text() == 'operator-owned unrelated bytes'
