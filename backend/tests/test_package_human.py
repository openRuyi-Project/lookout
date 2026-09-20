import json
from tracker import package


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
