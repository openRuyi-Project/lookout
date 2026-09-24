"""Ingestion and read-only projection share the same fact constraints."""

from copy import deepcopy

from hypothesis import given, strategies as st
import pytest

from tracker.api import Finding
from tracker import monitor_model


def item(value=False):
    return monitor_model.finding(
        "advisory", "Security", "Advisory",
        [monitor_model.evidence("KEV", value, "CISA", "https://www.cisa.gov/")],
        "https://example.org/advisory",
    )


def api_finding(raw):
    return Finding.model_validate({**raw, "monitor": "security", "stale": False})


@pytest.mark.parametrize("patch", [
    {"label": "bad label"}, {"label": "Security\n"}, {"label": 2},
    {"id": ""}, {"id": "x" * 8193}, {"title": False},
    {"evidence_url": "http://example.org"},
    {"evidence_url": "https://user:pass@example.org"},
    {"tags": ["bad tag"]}, {"tags": ["X"] * 9}, {"tags": ("KEV",)},
    {"facts": ()}, {"facts": [item()["facts"][0]] * 257},
    {"scope": "review"}, {"scope": "upgrade", "target_version": None},
    {"scope": "upgrade", "target_version": "MACRO_VERSION"},
    {"target_version": True}, {"resolution": "review this"},
])
def test_finding_constraints_agree_at_both_boundaries(patch):
    raw = {**item(), **patch}
    with pytest.raises(ValueError):
        monitor_model.validate_findings([raw])
    with pytest.raises(ValueError):
        api_finding(raw)


@pytest.mark.parametrize("patch", [
    {"key": ""}, {"source": "x" * 257}, {"code": "Not_A_Code"},
    {"code": "kev\n"}, {"status": "unknown"}, {"status": "unavailable"},
    {"url": "javascript:alert(1)"}, {"value": None},
    {"value": float("nan")}, {"value": float("inf")},
    {"value": b"false"}, {"value": [1]}, {"value": ("CVE-1",)},
    {"value": {"kev": False}}, {"value": "x" * 16385},
    {"severity": "urgent"},
])
def test_evidence_constraints_agree_at_both_boundaries(patch):
    raw = item()
    raw["facts"][0].update(patch)
    with pytest.raises(ValueError):
        monitor_model.validate_findings([raw])
    with pytest.raises(ValueError):
        api_finding(raw)


@pytest.mark.parametrize("status", ["unavailable", "not_applicable", "not_evaluated"])
def test_unknown_evidence_keeps_null_not_false(status):
    raw = item()
    raw["facts"][0].update(status=status, value=None)
    monitor_model.validate_findings([raw])
    assert api_finding(raw).facts[0].value is None


def test_raw_optional_code_is_omitted_but_legacy_api_null_roundtrips():
    raw = item()
    before = deepcopy(raw)
    monitor_model.validate_findings([raw])
    assert raw == before and "code" not in raw["facts"][0]
    response = api_finding(raw).model_dump()
    assert response["facts"][0]["code"] is None
    assert Finding.model_validate(response).model_dump() == response
    raw["facts"][0]["code"] = None
    with pytest.raises(ValueError):
        monitor_model.validate_findings([raw])


def test_required_raw_keys_duplicates_and_bounded_list():
    raw = item()
    for key in raw:
        missing = {name: value for name, value in raw.items() if name != key}
        with pytest.raises(ValueError):
            monitor_model.validate_findings([missing])
    for key in raw["facts"][0]:
        missing = deepcopy(raw)
        del missing["facts"][0][key]
        with pytest.raises(ValueError):
            monitor_model.validate_findings([missing])
    for invalid in (
        (raw,), [raw, raw], [raw] * 1001,
        [monitor_model.RawFinding.model_validate(raw)],
        [{**raw, "facts": [monitor_model.Evidence.model_validate(raw["facts"][0])]}],
    ):
        with pytest.raises(ValueError):
            monitor_model.validate_findings(invalid)


@given(st.one_of(
    st.booleans(), st.integers(min_value=-(2**63), max_value=2**63 - 1),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=100), st.lists(st.text(max_size=50), max_size=10),
))
def test_valid_provider_values_are_not_coerced_or_rewritten(value):
    raw = item(value)
    before = deepcopy(raw)
    monitor_model.validate_findings([raw])
    projected = api_finding(raw).facts[0].value
    assert type(projected) is type(value)
    assert projected == value
    assert raw == before
