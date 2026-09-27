import json

import pytest

from tests.helpers.config import setup_config
from tracker import config, package


@pytest.mark.parametrize('location', ['rule', 'policy'])
@pytest.mark.parametrize('candidate_value,runtime_value,matches', [
    ('true', '1', False), ('1', '1.0', False), ('true', 'true', True),
])
def test_explain_runtime_matches_preserve_toml_types(tmp_path, location, candidate_value, runtime_value, matches):
    candidate, runtime = [setup_config(tmp_path / name) for name in ('candidate', 'runtime')]
    for path, value in ((candidate, candidate_value), (runtime, runtime_value)):
        if location == 'rule':
            (path.parent / 'native.toml').write_text('[widget]\nsource="manual"\nmanual=' + value + '\n')
        else:
            (path.parent / 'packages.toml').write_text('[widget.monitors.fixture]\noption=' + value + '\n')
    result = package.explain(candidate, 'widget', runtime_config=runtime)
    if location == 'rule':
        assert result['rules'][0]['runtime_matches'] is matches
        assert result['runtime_binding_matches'] is True
    else:
        assert result['runtime_binding_matches'] is matches
        assert result['rules'][0]['runtime_matches'] is True


def test_human_check_links_saved_report_without_dumping_json(tmp_path, monkeypatch, capsys):
    result = {
        "name": "widget",
        "passed": True,
        "tracks": {"widget": {"version": "2.0", "error": None}},
        "state_writes": False,
    }
    monkeypatch.setattr(package, "check", lambda *a: result)
    report = tmp_path / "report.json"
    assert package.main(["check", "widget", "--config", "unused", "--output", str(report), "--format", "human"]) == 0
    output = capsys.readouterr().out
    assert "Check: passed" in output and str(report) in output
    assert "state_writes" not in output
    assert json.loads(report.read_text()) == result


def test_json_remains_default_and_failure_exit_is_preserved(monkeypatch, capsys):
    result = {"name": "widget", "passed": False, "tracks": {}}
    monkeypatch.setattr(package, "check", lambda *a: result)
    assert package.main(["check", "widget", "--config", "unused"]) == 2
    assert json.loads(capsys.readouterr().out) == result


def test_plan_and_apply_need_only_configuration_inputs(tmp_path, capsys):
    base, candidate, runtime = [setup_config(tmp_path / name) for name in ('base', 'candidate', 'runtime')]
    policy = candidate.parent / 'packages.toml'
    policy.write_text('[widget]\ntrack_label = "fixture-line"\n')
    review, prepared = tmp_path / 'review', tmp_path / 'prepared'
    assert package.main(['plan', '--base-config', str(base), '--config', str(candidate),
                         '--runtime-config', str(runtime), '--output', str(review)]) == 0
    assert json.loads(capsys.readouterr().out)['changed_bindings'] == ['widget']
    assert package.main(['apply', '--review', str(review), '--runtime-config', str(runtime),
                         '--destination', str(prepared)]) == 0
    assert config.load(prepared / 'tracker.toml')['packages']['widget']['track_label'] == 'fixture-line'


def test_plan_rejects_a_snapshot_argument_instead_of_ignoring_it(tmp_path, capsys):
    paths = [setup_config(tmp_path / name) for name in ('base', 'candidate', 'runtime')]
    review = tmp_path / 'review'
    with pytest.raises(SystemExit) as error:
        package.main(['plan', '--base-config', str(paths[0]), '--config', str(paths[1]),
                      '--runtime-config', str(paths[2]), '--output', str(review), '--db', 'unused.sqlite3'])
    assert error.value.code == 2
    assert 'unrecognized arguments: --db' in capsys.readouterr().err
    assert not review.exists()
