from pathlib import Path
import shutil
import pytest
from tracker import config_change, config
from test_architecture_repairs import setup_config

GROUPS = """# release policy belongs to the group
schema = 1
[group.registry]
source = "pypi"
pypi = "{name}"
packages = ["alpha", "beta"] # keep this explanation

# Independent maintenance line; do not regroup.
[group.other]
source = "git"
git = "https://example.org/{name}"
packages = ["gamma"]
"""


def configs(tmp_path):
    paths = []
    for name in ["base", "candidate", "runtime"]:
        path = setup_config(tmp_path / name)
        path.write_text(path.read_text().replace("native.toml", "versions/groups.toml"))
        (path.parent / "native.toml").unlink()
        (path.parent / "versions").mkdir()
        (path.parent / "versions/groups.toml").write_text(GROUPS)
        (path.parent / "versions/alpha.toml").write_text('# reason for override\nsource="pypi"\npypi="special"\n')
        paths.append(path)
    return paths


def test_unchanged_plan_preserves_all_authored_text(tmp_path):
    base, candidate, runtime = configs(tmp_path)
    review = tmp_path / "review"
    config_change.plan(base, candidate, runtime, review)
    for path in runtime.parent.rglob("*.toml"):
        assert (review / path.relative_to(runtime.parent)).read_bytes() == path.read_bytes()


def test_single_exception_edit_leaves_groups_untouched(tmp_path):
    base, candidate, runtime = configs(tmp_path)
    path = candidate.parent / "versions/alpha.toml"
    path.write_text(path.read_text().replace("special", "corrected"))
    review = tmp_path / "review"
    result = config_change.plan(base, candidate, runtime, review)
    assert result["changed_tracks"] == ["alpha"]
    assert (review / "versions/groups.toml").read_text() == GROUPS
    assert (review / "versions/alpha.toml").read_text() == path.read_text()


def test_group_change_reports_protected_override_and_keeps_operator_comment(tmp_path):
    base, candidate, runtime = configs(tmp_path)
    path = candidate.parent / "versions/groups.toml"
    path.write_text(GROUPS.replace('pypi = "{name}"', 'pypi = "new-{name}"'))
    operator = runtime.parent / "versions/groups.toml"
    operator.write_text(GROUPS.replace("Independent maintenance", "Operator-owned maintenance"))
    review = tmp_path / "review"
    result = config_change.plan(base, candidate, runtime, review)
    assert result["changed_tracks"] == ["beta"]
    assert result["overridden_unaffected_tracks"] == ["alpha"]
    assert (review / "versions/groups.toml").read_text() == path.read_text().replace(
        "Independent maintenance", "Operator-owned maintenance"
    )
    assert config.load(review / "tracker.toml")["native"]["beta"]["pypi"] == "new-beta"


def test_overlapping_literal_edit_is_not_silently_reformatted():
    with pytest.raises(ValueError, match="text changes conflict"):
        config_change.merge_text("a\nb\nc\n", "a\nx\nc\n", "a\ny\nc\n", "groups.toml")
