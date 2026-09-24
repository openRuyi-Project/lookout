"""Compare SPDX expressions, not text order or legal compatibility."""

from hypothesis import given, strategies as st
import pytest

from tracker import monitor_license


class Metadata:
    def __init__(self, old, new):
        self.expressions = iter((old, new))
        self.calls = []

    def json(self, method, url):
        self.calls.append((method, url))
        return {"info": {"license_expression": next(self.expressions)}}


def check(old, new):
    return monitor_license.check(
        {"version": "1.0", "target_version": "2.0"},
        {"pypi": "fixture"}, Metadata(old, new),
    )


@pytest.mark.parametrize("old,new", [
    ("MIT OR Apache-2.0", "Apache-2.0 OR MIT"),
    ("MIT AND Apache-2.0", "Apache-2.0 AND MIT"),
    ("MIT OR (Apache-2.0 OR ISC)", "(ISC OR MIT) OR Apache-2.0"),
    ("MIT", "MIT OR MIT"),
    ("GPL-2.0-only WITH Classpath-exception-2.0 OR MIT",
     "MIT OR GPL-2.0-only WITH Classpath-exception-2.0"),
])
def test_equivalent_expressions_do_not_raise_license_change(old, new):
    result = check(old, new)
    assert result["status"] == "ok" and result["findings"] == []


@pytest.mark.parametrize("old,new", [
    ("MIT", "Apache-2.0"),
    ("MIT OR Apache-2.0", "MIT AND Apache-2.0"),
    ("GPL-2.0-only WITH Classpath-exception-2.0", "GPL-2.0-only"),
    ("LicenseRef-local", "LicenseRef-other"),
])
def test_distinct_expressions_preserve_upgrade_and_source_evidence(old, new):
    finding, = check(old, new)["findings"]
    assert finding["scope"] == "upgrade" and finding["target_version"] == "2.0"
    assert [fact["value"] for fact in finding["facts"]] == [old, new]
    assert all(fact["source"] == "PyPI" for fact in finding["facts"])
    assert "severity" not in finding and "resolution" not in finding


def test_evidence_keeps_original_provider_expression():
    finding, = check("mit", "(Apache-2.0)")["findings"]
    assert [fact["value"] for fact in finding["facts"]] == ["mit", "(Apache-2.0)"]
    assert finding["title"] == "MIT → (Apache-2.0)"


@pytest.mark.parametrize("expression", [
    None, "", "this is not SPDX", "MIT OR", ["MIT"], 42,
    "(" * 17 + "MIT" + ")" * 17,
    " OR ".join(["MIT"] * 65), "LicenseRef-" + "x" * 2048,
])
def test_missing_invalid_and_over_budget_metadata_does_not_mean_unchanged(expression, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("untrusted metadata reached equivalence simplification")

    monkeypatch.setattr(monitor_license._LICENSING, "is_equivalent", unexpected)
    result = check("MIT", expression)
    assert result["status"] == "unsupported" and result["findings"] == []


@given(st.lists(st.sampled_from(["MIT", "Apache-2.0", "BSD-2-Clause", "ISC"]),
                min_size=1, max_size=8), st.sampled_from([" OR ", " AND "]))
def test_reordering_and_duplicate_terms_are_invariant(licenses, operator):
    original = operator.join(licenses)
    reordered = operator.join(reversed(licenses + licenses))
    result = check(original, reordered)
    assert result["status"] == "ok" and result["findings"] == []
