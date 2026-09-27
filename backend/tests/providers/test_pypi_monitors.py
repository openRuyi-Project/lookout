from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from tracker import state
from tracker.monitors import (
    license as monitor_license,
    model as monitor_model,
    runner as monitor,
    yanked as monitor_yanked,
)
from tracker.monitors.requires import model as requirements, monitor as monitor_requires
from tracker.providers.client import IO


SUBJECT = {"name": "python-fixture", "version": "1.0", "target_version": "2.0"}
SETTINGS = {"pypi": "upstream-fixture"}


class ReleaseIO:
    def __init__(self, current, target=None):
        self.responses = {"1.0": current, "2.0": current if target is None else target}

    def json(self, method, url):
        assert method == "GET"
        assert url.startswith("https://pypi.org/pypi/upstream-fixture/")
        version = url.split("/")[-2]
        return {"info": {"name": "upstream-fixture", "version": version, **deepcopy(self.responses[version])}}


@pytest.mark.parametrize("adapter", [monitor_license, monitor_yanked, monitor_requires])
def test_pypi_identity_reused_without_guessing_from_rpm_name(adapter):
    package = {"name": "python-not-the-identity", "identity": {"source": "pypi", "pypi": "actual-project"}}
    assert adapter.inputs(package, None) == {"pypi": "actual-project"}
    assert adapter.inputs({**package, "identity": {"source": "manual"}}, None) is None
    assert adapter.inputs(package, {"pypi": "reviewed-exception"}) == {"pypi": "reviewed-exception"}
    with pytest.raises(ValueError, match="identity"):
        adapter.inputs(package, {"pypi": "actual-project", "extra": True})


def test_yanked_release_reports_source_reason_and_version():
    result = monitor_yanked.check(SUBJECT, SETTINGS, ReleaseIO({"yanked": True, "yanked_reason": "Broken sdist"}))
    assert result["status"] == "ok"
    finding = result["findings"][0]
    assert (finding["label"], finding["title"], finding["scope"]) == ("Yanked", "upstream-fixture 1.0", "current")
    assert [(fact["code"], fact["value"]) for fact in finding["facts"]] == [
        ("release_yanked", True), ("yanked_reason", "Broken sdist")]
    assert all(fact["source"] == "PyPI" and fact["url"].endswith("/1.0/json") for fact in finding["facts"])


@pytest.mark.parametrize("yanked", [None, 0, 1, "false"])
def test_yanked_missing_or_invalid_assertion_is_not_a_negative(yanked):
    result = monitor_yanked.check(SUBJECT, SETTINGS, ReleaseIO({"yanked": yanked}))
    assert result["status"] == "unsupported" and result["findings"] == []


@pytest.mark.parametrize("info, expected", [({}, "unsupported"), ({"yanked": False}, "ok")])
def test_individual_file_yanks_do_not_imply_release_yanked(info, expected):
    class FilesIO:
        def json(self, method, url):
            return {"info": {"name": "upstream-fixture", "version": "1.0", **info},
                    "urls": [{"yanked": True}, {"yanked": False}]}

    result = monitor_yanked.check(SUBJECT, SETTINGS, FilesIO())
    assert result["status"] == expected and result["findings"] == []


@pytest.mark.parametrize("reason", [None, "", " "])
def test_yanked_without_reason_does_not_invent_one(reason):
    result = monitor_yanked.check(SUBJECT, SETTINGS, ReleaseIO({"yanked": True, "yanked_reason": reason}))
    assert len(result["findings"][0]["facts"]) == 1


@pytest.mark.parametrize("reason", [True, "x" * 1025])
def test_yanked_rejects_invalid_or_unbounded_reason(reason):
    with pytest.raises(ValueError, match="Registry yanked reason"):
        monitor_yanked.check(SUBJECT, SETTINGS, ReleaseIO({"yanked": True, "yanked_reason": reason}))


def test_python_requirements_are_independent_attributed_release_facts_not_assessments():
    result = monitor_requires.check(SUBJECT, SETTINGS,
        ReleaseIO({"requires_python": " >=3.8 "}, {"requires_python": ">=3.10"}))
    assert result["status"] == "ok"
    assert result["scope_checks"] == {
        "current": {"status": "ok", "note": None}, "upgrade": {"status": "ok", "note": None}}
    current, target = result["findings"]
    assert (current["title"], current["scope"], current["target_version"]) == ("Python >=3.8", "current", None)
    assert (target["title"], target["scope"], target["target_version"]) == ("Python >=3.10", "upgrade", "2.0")
    assert current['requirement']['constraint'] == {
        'expression': ' >=3.8 ', 'source': 'PyPI', 'url': 'https://pypi.org/pypi/upstream-fixture/1.0/json'}
    assert target['requirement']['constraint'] == {
        'expression': '>=3.10', 'source': 'PyPI', 'url': 'https://pypi.org/pypi/upstream-fixture/2.0/json'}
    for finding in (current, target):
        assert finding["label"] == "Requires" and finding['facts'] == []
        fact = finding['requirement']
        assert (fact['dependency'], fact['name'], fact['kind'], fact['scheme']) == ('python', 'Python', 'runtime', 'pep440')
        assert not {"severity", "resolution", "action"} & finding.keys()
        assert not {"current", "target", "satisfaction", "changed"} & fact.keys()


@pytest.mark.parametrize("old,new", [
    (">=3.8,<4", "<4, >=3.8"),
    (">=3.8,>=3.8", ">=3.8"),
    (">=3.8.0", ">=3.8"),
])
def test_python_requirement_normalization_prevents_order_or_format_only_drift(old, new, snapshot):
    result = monitor_requires.check(SUBJECT, SETTINGS,
        ReleaseIO({"requires_python": old}, {"requires_python": new}))
    assert result["status"] == "ok" and len(result["findings"]) == 2
    assert requirements.equivalent("pep440", old, new)
    projected, = requirements.project([{**finding, "stale": False} for finding in result["findings"]],
                                      snapshot, datetime.now(timezone.utc))
    assert not projected["changed"]
    assert projected["current"]["expression"] == old and projected["target"]["expression"] == new


@pytest.mark.parametrize("declaration", [None, "", " ", 3, "Python 3", ">=3.8," * 65, "x" * 1025])
@pytest.mark.parametrize("position", ["current", "target"])
def test_missing_or_invalid_python_declaration_does_not_hide_the_other_release(declaration, position):
    current, target = {"requires_python": ">=3.8"}, {"requires_python": ">=3.10"}
    (current if position == "current" else target)["requires_python"] = declaration
    result = monitor_requires.check(SUBJECT, SETTINGS, ReleaseIO(current, target))
    assert result["status"] == "partial"
    failed_scope, valid_scope = ("current", "upgrade") if position == "current" else ("upgrade", "current")
    assert result["scope_checks"][failed_scope]["status"] == "unsupported"
    assert result["scope_checks"][failed_scope]["note"]
    assert result["scope_checks"][valid_scope] == {"status": "ok", "note": None}
    finding, = result["findings"]
    assert finding["scope"] == valid_scope
    assert finding["requirement"]["constraint"]["expression"] == (">=3.10" if position == "current" else ">=3.8")


def test_current_requirements_are_observed_without_an_upgrade_target():
    result = monitor_requires.check({"version": "1.0"}, SETTINGS, ReleaseIO({"requires_python": ">=3.8"}))
    assert result["status"] == "ok"
    assert result["scope_checks"] == {"current": {"status": "ok", "note": None}}
    finding, = result["findings"]
    assert finding["scope"] == "current" and finding["target_version"] is None
    assert finding["requirement"]["constraint"]["expression"] == ">=3.8"


def test_pypi_monitors_share_http_cache_without_coupling_to_license_availability(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        version = request.url.path.split("/")[-2]
        return httpx.Response(200, json={"info": {
            "name": "upstream-fixture", "version": version,
            "requires_python": ">=3.8" if version == "1.0" else ">=3.10",
            "yanked": version == "1.0", "yanked_reason": "Broken sdist"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        io = IO(tmp_path / "cache", client=client)
        adapters = [monitor_license, monitor_yanked, monitor_requires]
        with ThreadPoolExecutor(max_workers=3) as workers:
            results = list(workers.map(
                lambda adapter: adapter.check(SUBJECT, SETTINGS, io.for_hosts(adapter.HOSTS)), adapters))
        assert len(calls) == 2
        assert {url.split("/")[-2] for url in calls} == {"1.0", "2.0"}
        assert results[0]["status"] == "unsupported"  # No SPDX metadata does not gate other checks.
        assert results[1]["findings"][0]["label"] == "Yanked"
        assert results[2]["findings"][0]["label"] == "Requires"
        # A later runner instance uses the same dated transport cache.
        later_io = IO(tmp_path / "cache", client=client)
        assert monitor_yanked.check(SUBJECT, SETTINGS, later_io.for_hosts(monitor_yanked.HOSTS)) == results[1]
        assert len(calls) == 2


def test_independent_scopes_and_refresh_policies(config, snapshot, monkeypatch):
    config["native"]["binutils"] = {"source": "pypi", "pypi": "upstream-fixture"}
    monkeypatch.setattr(state, "compare", lambda *args: "current")
    yanked = monitor.plan(config, snapshot, "binutils", "yanked")
    requires = monitor.plan(config, snapshot, "binutils", "requires")
    assert yanked["status"] == "pending" and yanked["scope"] == "current"
    assert "target_version" not in yanked["subject"]
    assert requires["status"] == "pending" and requires["scope"] == "current_and_upgrade"
    assert requires["subject"]["target_version"] is None
    monkeypatch.setattr(state, "compare", lambda *args: "outdated")
    requires = monitor.plan(config, snapshot, "binutils", "requires")
    assert requires["status"] == "pending"
    assert requires["subject"]["target_version"] == "3.10.0"
    assert monitor.refresh_policy("yanked", yanked, {}, {}).interval_seconds == 21600
    assert monitor.refresh_policy("requires", requires, {}, {}).interval_seconds == 43200
    snapshot["sources"]["binutils"]["srcmd5"] = "patch-only-change"
    assert monitor.plan(config, snapshot, "binutils", "yanked")["fingerprint"] == yanked["fingerprint"]


@pytest.mark.parametrize("provider", ["yanked", "requires"])
@pytest.mark.parametrize("failure", ["network", "malformed", "identity"])
def test_failed_checks_retain_matching_previous_evidence(config, snapshot, monkeypatch, provider, failure):
    config["native"]["binutils"] = {"source": "pypi", "pypi": "upstream-fixture"}
    monkeypatch.setattr(state, "compare", lambda *args: "outdated")
    response = "healthy"

    def handler(request):
        if response == "network":
            raise httpx.ConnectError("offline")
        if response == "malformed":
            return httpx.Response(200, json={"info": None})
        version = request.url.path.split("/")[-2]
        return httpx.Response(200, json={"info": {
            "name": "other-project" if response == "identity" else "upstream-fixture", "version": version,
            "yanked": True, "requires_python": ">=3.8" if version == "3.9.0" else ">=3.10"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        io = IO(client=client, ttl=0)
        proposed = monitor.plan(config, snapshot, "binutils", provider)
        previous = monitor.execute(provider, proposed, io)
        assert previous["status"] == "ok" and previous["findings"]
        response = failure
        failed = monitor.execute(provider, proposed, io, previous)
        assert failed["status"] == "error"
        if provider == "requires":
            assert {scope: check["status"] for scope, check in failed["scope_checks"].items()} == {
                "current": "error", "upgrade": "error"}
            for scope in ("current", "upgrade"):
                assert failed["scope_checks"][scope]["checked_at"] == previous["scope_checks"][scope]["checked_at"]
                assert failed["scope_checks"][scope]["attempted_at"] >= previous["scope_checks"][scope]["attempted_at"]
        for key in ("findings", "checked_at", "changed_at", "evidence_revision"):
            assert failed[key] == previous[key]
        snapshot["monitors"] = {"binutils": {provider: failed}}
        projected = monitor_model.project(snapshot, "binutils", datetime.now(timezone.utc))
        assert len(projected["findings"]) == len(previous["findings"])
        assert all(finding["stale"] for finding in projected["findings"])
        changed = {**proposed, "fingerprint": "different-version-pair",
                   "subject": {**proposed["subject"], "version": "different-release"}}
        assert monitor.execute(provider, changed, io, previous)["findings"] == []


@pytest.mark.parametrize("failed_scope", ["current", "upgrade"])
def test_failed_release_retains_only_its_dated_evidence_while_other_release_refreshes(
        config, snapshot, monkeypatch, failed_scope):
    config["native"]["binutils"] = {"source": "pypi", "pypi": "upstream-fixture"}
    monkeypatch.setattr(state, "compare", lambda *args: "outdated")
    first = datetime.now(timezone.utc)
    first_at, retry_at = first.isoformat(), (first + timedelta(minutes=1)).isoformat()
    fail = False

    def handler(request):
        scope = "current" if "/3.9.0/" in request.url.path else "upgrade"
        if fail and scope == failed_scope:
            raise httpx.ConnectError("offline for this release")
        return httpx.Response(200, json={"info": {
            "name": "upstream-fixture", "version": request.url.path.split("/")[-2],
            "requires_python": ">=3.8" if scope == "current" else ">=3.10"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        io = IO(client=client, ttl=0)
        proposed = monitor.plan(config, snapshot, "binutils", "requires")
        monkeypatch.setattr(state, "utcnow", lambda: first_at)
        previous = monitor.execute("requires", proposed, io)
        assert previous["status"] == "ok"
        fail = True
        monkeypatch.setattr(state, "utcnow", lambda: retry_at)
        retried = monitor.execute("requires", proposed, io, previous)
        assert retried["status"] == "partial" and retried["checked_at"] == retry_at
        assert retried["findings"] == previous["findings"]
        successful_scope = "upgrade" if failed_scope == "current" else "current"
        assert retried["scope_checks"][failed_scope]["status"] == "error"
        assert retried["scope_checks"][failed_scope]["checked_at"] == first_at
        assert retried["scope_checks"][successful_scope]["status"] == "ok"
        assert retried["scope_checks"][successful_scope]["checked_at"] == retry_at
        assert all(check["attempted_at"] == retry_at for check in retried["scope_checks"].values())
        snapshot["monitors"] = {"binutils": {"requires": retried}}
        projected = monitor_model.project(snapshot, "binutils", datetime.fromisoformat(retry_at))
        assert {finding["scope"]: finding["stale"] for finding in projected["findings"]} == {
            failed_scope: True, successful_scope: False}


@pytest.mark.parametrize('info', [
    {'name': 'other-project', 'version': '1.0'},
    {'name': 'upstream-fixture', 'version': '9.0'},
    {'name': 'upstream-fixture'},
    {'version': '1.0'},
    {'name': None, 'version': '1.0'},
    {'name': 'upstream-fixture', 'version': 1},
    {'name': 'upstream-fixture', 'version': 'not-a-release'},
])
def test_pypi_release_rejects_missing_or_mismatched_identity(info):
    from tracker.providers.pypi import release

    class MetadataIO:
        def json(self, method, url):
            return {'info': info}

    with pytest.raises(ValueError, match='identity'):
        release('upstream-fixture', '1.0', MetadataIO())


@pytest.mark.parametrize('version, observed', [
    ('1.0', '1.0.0'),
    ('1.0rc1', '1.0RC1'),
    ('1.0+linux', '1.0+LINUX'),
    ('legacy-release', 'legacy-release'),
])
def test_pypi_release_keeps_equivalent_identity_and_literal_evidence(version, observed):
    from tracker.providers.pypi import release

    info = {'name': 'Upstream_Fixture', 'version': observed, 'yanked': False}

    class MetadataIO:
        def json(self, method, url):
            return {'info': info}

    result, url = release('upstream-fixture', version, MetadataIO())
    assert result is info
    assert url == f'https://pypi.org/pypi/upstream-fixture/{version.replace("+", "%2B")}/json'
