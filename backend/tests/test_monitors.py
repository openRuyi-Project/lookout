from copy import deepcopy
from datetime import date, datetime, timezone
import types
import pytest
from tracker import monitor, monitor_eol, monitor_security, monitor_model, state, view


class FixtureIO:
    today = date(2026, 9, 20)

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def for_hosts(self, hosts):
        return self

    def json(self, method, url, body=None):
        self.calls.append((method, url, body))
        for key, response in self.responses.items():
            if key in url:
                if isinstance(response, Exception):
                    raise response
                return deepcopy(response)
        raise AssertionError(url)


def test_eol_current_cycle_not_active_support():
    io = FixtureIO({'endoflife': {'result': {'releases': [
        {'name': '3.6', 'isEol': False, 'isMaintained': False, 'eolFrom': '2027-01-01'},
        {'name': '1.0', 'isEol': True, 'eolFrom': '2020-01-01'}]}}})
    settings = {'product': 'fixture', 'cycle_parts': 2}
    assert monitor_eol.check({'version': '3.6.3'}, settings, io)['findings'] == []
    result = monitor_eol.check({'version': '1.0.9'}, settings, io)
    assert result['findings'][0]['label'] == 'EOL'
    assert monitor_eol.check({'version': '9.9.1'}, settings, io)['status'] == 'unsupported'
    assert monitor_eol.check({'version': 'git123'}, settings, io)['status'] == 'unsupported'


def test_security_identity_is_not_guessed(config, snapshot):
    config['native']['python-foo'] = {'source': 'regex', 'url': 'https://example.org'}
    assert monitor_security.inputs({'identity': config['native']['python-foo']}, None) is None
    config['native']['python-foo'] = {'source': 'pypi', 'pypi': 'actual-name'}
    assert monitor_security.inputs({'identity': config['native']['python-foo']}, None) == {'ecosystem': 'PyPI', 'name': 'actual-name'}


def test_security_alias_dedup_and_attributed_kev_epss():
    item = {"id": "GHSA-fixture", "aliases": ["CVE-2026-12345"], "affected": []}
    io = FixtureIO(
        {
            "/v1/query": {
                "vulns": [
                    item,
                    {**item, "id": "CVE-2026-12345"},
                    {"id": "withdrawn", "affected": [], "withdrawn": "2026-01-01"},
                ]
            },
            "cisa.gov": {"vulnerabilities": [{"cveID": "CVE-2026-12345"}]},
            "first.org": {"data": [{"cve": "CVE-2026-12345", "epss": "0.92", "date": "2026-09-20"}]},
        }
    )
    result = monitor_security.check({"version": "1.0"}, {"ecosystem": "PyPI", "name": "fixture"}, io)
    assert result["status"] == "ok" and len(result["findings"]) == 1
    f = result["findings"][0]
    assert f["label"] == "Security" and f["tags"] == ["KEV"]
    facts = {fact["key"]: fact["value"] for fact in f["facts"]}
    assert facts["EPSS probability · CVE-2026-12345"] == 0.92
    assert facts["EPSS model date · CVE-2026-12345"] == "2026-09-20"
    assert facts["Query version"] == "1.0"
    assert not {"severity", "resolution", "detail"} & f.keys()
    assert f["evidence_url"].endswith("CVE-2026-12345")
    assert monitor_model.summarize([{**f, "stale": False}])[1]["label"] == "KEV"


def test_enrichment_failure_keeps_security_candidate():
    io = FixtureIO({'/v1/query': {'vulns': [{'id': 'CVE-2026-12345', 'affected': []}]},
                    'cisa.gov': ValueError('offline'), 'first.org': ValueError('offline')})
    result = monitor_security.check({'version': '1.0'}, {'ecosystem': 'PyPI', 'name': 'fixture'}, io)
    assert result['status'] == 'partial' and len(result['findings']) == 1
    assert result['findings'][0]['tags'] == []


def test_error_retains_only_matching_inputs(config, snapshot, monkeypatch):
    config["packages"]["binutils"] = {"monitors": {"eol": {"product": "fixture", "cycle_parts": 1}}}
    proposed = monitor.plan(config, snapshot, "binutils", "eol")
    previous = {
        **proposed,
        "checked_at": state.utcnow(),
        "findings": [monitor_model.finding("test", "EOL", "Test", [], "https://example.org/")],
    }
    out = monitor.execute("eol", proposed, FixtureIO({"endoflife": ValueError("offline")}), previous)
    assert out["status"] == "error" and out["findings"] == previous["findings"]
    assert out["checked_at"] == previous["checked_at"]
    out = monitor.execute(
        "eol", {**proposed, "fingerprint": "different"}, FixtureIO({"endoflife": ValueError()}), previous
    )
    assert out["findings"] == [] and out["checked_at"] is None


def test_new_monitor_uses_existing_runner_projection_and_api(config, snapshot, monkeypatch, tmp_path):
    # Actual third adapter registration, not a mock frontend branch. A new label
    # must flow through the unchanged generic storage/projection/API/filter path.
    fixture = types.SimpleNamespace(
        VERSION=1,
        HOSTS={"example.org"},
        inputs=lambda package, configured: {"identity": package["name"]},
        check=lambda subject, inputs, io: {
            "status": "ok",
            "note": "fixture",
            "findings": [monitor_model.finding("one", "NewSignal", "New observation", [], "https://example.org/")],
        },
    )
    monkeypatch.setitem(monitor.REGISTRY, "newsignal", fixture)
    config["monitors"] = {"enabled": ["newsignal"]}
    fact = monitor.check(config, snapshot, "binutils", "newsignal", FixtureIO({}))
    assert fact["status"] == "ok"
    snapshot["monitors"] = {"binutils": {"newsignal": fact}}
    from tracker.api import create_app
    from fastapi.testclient import TestClient

    db = tmp_path / "state.db"
    state.commit(db, snapshot)
    client = TestClient(create_app(db))
    out = client.get("/api/v1/packages?maintenance=NewSignal").json()
    assert out["total"] == 1 and out["items"][0]["name"] == "binutils"
    assert out["maintenance_labels"] == {"NewSignal": 1}
    detail = client.get("/api/v1/packages/binutils").json()
    assert detail["maintenance_findings"][0]["title"] == "New observation"


def test_upgrade_monitor_never_runs_without_upgrade(config, snapshot, monkeypatch):
    fixture = types.SimpleNamespace(
        VERSION=1,
        SCOPE="upgrade",
        HOSTS={"example.org"},
        inputs=lambda package, configured: {"identity": package["name"]},
        check=lambda *args: pytest.fail("not an upgrade"),
    )
    monkeypatch.setitem(monitor.REGISTRY, "license", fixture)
    monkeypatch.setattr(state, "compare", lambda *args: "current")
    fact = monitor.check(config, snapshot, "binutils", "license", FixtureIO({}))
    assert fact["status"] == "not_applicable"
    monkeypatch.setattr(state, "compare", lambda *args: "outdated")
    fixture.check = lambda s, p, io: {
        "status": "ok",
        "note": "fixture",
        "findings": [
            monitor_model.finding(
                "license",
                "LicenseChange",
                "License changed",
                [],
                "https://example.org/",
                scope="upgrade",
                target_version=s["target_version"],
            )
        ],
    }
    fact = monitor.check(config, snapshot, "binutils", "license", FixtureIO({}))
    snapshot["monitors"] = {"binutils": {"license": fact}}
    now = datetime.now(timezone.utc)
    assert monitor_model.project(snapshot, "binutils", now, "3.10.0", True)["summary"][0]["label"] == "LicenseChange"
    assert monitor_model.project(snapshot, "binutils", now, "3.10.0", False)["summary"] == []
    assert monitor_model.project(snapshot, "binutils", now, "3.11.0", True)["summary"] == []


def test_source_patch_revision_invalidates_finding(config, snapshot):
    config["packages"]["binutils"] = {"monitors": {"eol": {"product": "fixture", "cycle_parts": 1}}}
    proposed = monitor.plan(config, snapshot, "binutils", "eol")
    proposed.update(
        status="ok",
        checked_at=state.utcnow(),
        findings=[monitor_model.finding("id", "EOL", "EOL", [], "https://example.org/")],
    )
    snapshot["monitors"] = {"binutils": {"eol": proposed}}
    snapshot["sources"]["binutils"]["srcmd5"] = "patch-changed"
    assert monitor_model.project(snapshot, "binutils", datetime.now(timezone.utc))["findings"] == []


def test_monitor_contract_rejects_unsafe_and_redundant_identifiers():
    with pytest.raises(ValueError):
        monitor_model.finding("id", "two words", "x", [], "https://example.org/")
    with pytest.raises(ValueError):
        monitor_model.finding("id", "Signal", "x", [], "javascript:alert(1)")
    with pytest.raises(ValueError):
        monitor_model.finding("id", "Signal", "x", [], "https://example.org/", scope="upgrade")


def test_buildsystem_configuration_not_frontend_categories(snapshot, tmp_path):
    from fastapi.testclient import TestClient
    from tracker.api import create_app
    snapshot['specs']['binutils'] = {'metadata': {'name': 'binutils', 'buildsystem': 'new-buildsystem'}}
    snapshot['specs']['foo3'] = {'metadata': {'name': 'foo3', 'buildsystem': None}}
    snapshot['presentation'] = {'buildsystems': {'new-buildsystem': {'background': '#123456', 'foreground': '#ffffff'}}}
    db = tmp_path/'state.db';state.commit(db, snapshot);client = TestClient(create_app(db))
    result = client.get('/api/v1/packages?buildsystem=new-buildsystem').json()
    assert result['total'] == 1 and result['items'][0]['buildsystem'] == 'new-buildsystem'
    assert result['presentation'] == snapshot['presentation']
    missing = client.get('/api/v1/packages?buildsystem=_not_detected').json()
    assert missing['total'] == 4
    assert missing['buildsystems']['_not_detected'] == 4
    assert {p['buildsystem_status'] for p in missing['items']} == {'not_declared', 'unknown'}


def test_monitor_phase_cannot_overwrite_other_observations(snapshot):
    with pytest.raises(ValueError): state.merge(snapshot, 'monitors', {'tracks': {}})
    with pytest.raises(ValueError): state.merge(snapshot, 'monitors', {}, {'spec_git': {}})
    new = state.merge(snapshot, 'monitors', {'monitors': {'x': {}}})
    assert new['tracks'] == snapshot['tracks'] and new['specs'] == snapshot['specs']


def test_real_license_adapter_compares_same_pair_and_requires_spdx():
    from tracker import monitor_license
    subject = {'version': '1.0', 'target_version': '2.0'}
    io = FixtureIO({'/1.0/': {'info': {'license_expression': 'MIT'}},
                    '/2.0/': {'info': {'license_expression': 'Apache-2.0'}}})
    result = monitor_license.check(subject, {'pypi': 'fixture'}, io)
    assert result['findings'][0]['label'] == 'LicenseChange'
    assert result['findings'][0]['target_version'] == '2.0'
    io.responses['/2.0/']['info']['license_expression'] = 'mit'
    assert monitor_license.check(subject, {'pypi': 'fixture'}, io)['findings'] == []
    io.responses['/2.0/']['info'] = {'license': 'MIT'}
    assert monitor_license.check(subject, {'pypi': 'fixture'}, io)['status'] == 'unsupported'


def test_scanner_report_requires_fresh_database_and_exact_identity():
    from tracker.monitor_cve import parse_report, ScannerDatabaseExpired
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    report = {'metadata': {'tool': {'name': 'cve-bin-tool'}},
              'database_info': {'last_updated': '2026-09-20 00:00:00'},
              'vulnerabilities': {'report': [{'entries': [{'vendor': 'gnu', 'product': 'bash',
                  'version': '5.3', 'cve_number': 'CVE-2026-12345'}]}]}}
    assert parse_report(report, {'vendor': 'gnu', 'product': 'bash'}, '5.3', now)[0]['id'] == 'CVE-2026-12345'
    with pytest.raises(ValueError): parse_report(report, {'vendor': 'gnu', 'product': 'bash'}, '5.2', now)
    report['database_info']['last_updated'] = '2026-01-01 00:00:00'
    with pytest.raises(ScannerDatabaseExpired): parse_report(report, {'vendor': 'gnu', 'product': 'bash'}, '5.3', now)


def test_maintenance_tags_follow_same_single_word_contract():
    with pytest.raises(ValueError):
        monitor_model.finding("test", "Review", "Review", [], "https://example.org/", tags=["two words"])


def test_io_host_boundary_and_shared_cache(tmp_path):
    import httpx
    from tracker.monitor_io import IO
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={'ok': True})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        owner = IO(tmp_path, client=client)
        io = owner.for_hosts({'example.org'})
        assert io.json('GET', 'https://example.org/fact') == {'ok': True}
        assert io.json('GET', 'https://example.org/fact') == {'ok': True}
        assert len(calls) == 1
        for url in ['http://example.org/fact', 'https://elsewhere.org/fact', 'https://user@example.org/fact', 'https://example.org:444/fact']:
            with pytest.raises(ValueError): io.json('GET', url)
        second = IO(tmp_path, client=client).for_hosts({'example.org'})
        assert second.json('GET', 'https://example.org/fact') == {'ok': True}
        assert len(calls) == 1


def test_monitor_batch_fairness_and_partial_publication(config, snapshot, monkeypatch, tmp_path):
    from collections import Counter
    calls, published = [], []
    for provider in ('first', 'second'):
        def check(subject, inputs, io, provider=provider):
            calls.append(provider)
            return {'status': 'ok', 'findings': [], 'note': 'fixture'}
        monkeypatch.setitem(monitor.REGISTRY, provider, types.SimpleNamespace(
            VERSION=1, HOSTS=set(), inputs=lambda package, configured: {}, check=check))
    config.update(monitors={'enabled':['first','second'], 'batch_size':2, 'workers':1}, config_digest='fixture', nv_digest='fixture')
    monkeypatch.setattr(monitor.cfg, 'load', lambda path: config)
    db = tmp_path/'state.db';state.commit(db, snapshot)
    commit = state.commit
    def capture(path, new):
        published.append(deepcopy(new))
        return commit(path, new)
    monkeypatch.setattr(state, 'commit', capture)
    result = monitor.collect(config, 'unused', db, io=FixtureIO({}))
    assert Counter(calls) == {'first': 1, 'second': 1}
    assert len(published) >= 3
    assert any(f['status'] == 'pending' for ps in published[0]['monitors'].values() for f in ps.values())
    assert sum(f['status'] == 'ok' for ps in result['monitors'].values() for f in ps.values()) == 2
    assert result['sources'] == snapshot['sources'] and result['tracks'] == snapshot['tracks']


def test_monitor_proxy_is_operator_scoped(monkeypatch):
    from tracker import monitor_io
    calls = []
    monkeypatch.setenv('TRACKER_MONITOR_PROXY', 'http://127.0.0.1:7890')
    monkeypatch.setattr(monitor_io.httpx, 'Client', lambda **kwargs: calls.append(kwargs) or types.SimpleNamespace(close=lambda: None))
    owner = monitor_io.IO();owner.close()
    assert calls == [{'timeout':15, 'follow_redirects':False, 'proxy':'http://127.0.0.1:7890'}]


@pytest.mark.parametrize('entry,expected', [
    ({'source':'regex','url':'https://index.crates.io/se/rd/serde'}, {'ecosystem':'crates.io','name':'serde'}),
    ({'source':'regex','url':'https://index.crates.io/3/s/syn'}, {'ecosystem':'crates.io','name':'syn'}),
    ({'source':'regex','url':'https://index.crates.io/wrong/serde'}, None),
    ({'source':'jq','url':'https://proxy.golang.com.cn/github.com/!burnt!sushi/toml/@latest'}, {'ecosystem':'Go','name':'github.com/BurntSushi/toml'}),
    ({'source':'jq','url':'https://example.org/github.com/name/project/@latest'}, None),
    ({'source':'github','github':'name/project'}, None),
])
def test_native_registry_identity_without_package_name_guess(entry, expected):
    from tracker.package_identity import from_native
    assert from_native(entry) == expected
    assert monitor_security.inputs({'identity':entry}, None) == expected


def test_security_snapshot_version_is_not_silently_coerced():
    assert monitor_security.osv({'version':'0.0.0^git20260920'}, {'ecosystem':'Go','name':'example.org/module'}, FixtureIO({})) is None


def test_facets_follow_search_other_filter_and_view_not_pagination(snapshot, tmp_path):
    from tracker.api import create_app
    from fastapi.testclient import TestClient

    for name, buildsystem, labels in [
        ("binutils", "cmake", ["EOL"]),
        ("foo3", "cmake", ["Security"]),
        ("foo4", "meson", ["EOL", "Security"]),
    ]:
        snapshot["specs"][name] = state.success({}, {"head": "spec-" + name, "metadata": {"name": name, "buildsystem": buildsystem, "version": snapshot["sources"][name]["version"]}}, state.utcnow())
        snapshot.setdefault("monitors", {})[name] = {
            "fixture": {
                "subject": monitor_model.subject(snapshot, name),
                "scope": "current",
                "status": "ok",
                "checked_at": state.utcnow(),
                "findings": [
                    monitor_model.finding(label, label, label, [], "https://example.org/") for label in labels
                ],
            }
        }
    db = tmp_path / "state.db"
    state.commit(db, snapshot)
    client = TestClient(create_app(db))
    out = client.get("/api/v1/packages?buildsystem=cmake&maintenance=EOL&per_page=1&page=2").json()
    assert out["total"] == 1 and out["items"][0]["name"] == "binutils"
    assert out["maintenance_labels"] == {"EOL": 1, "Security": 1}
    assert out["buildsystems"] == {"cmake": 1, "meson": 1}
    assert out["counts"]["all"] == out["counts"]["updates"] == 1 and out["counts"]["problems"] == 0
    out = client.get("/api/v1/packages?buildsystem=cmake&view=updates").json()
    assert out["maintenance_labels"] == {"EOL": 1} and out["counts"]["all"] == 2 and out["total"] == 1
    out = client.get("/api/v1/packages?q=foo&buildsystem=cmake&maintenance=EOL").json()
    assert out["total"] == 0 and out["maintenance_labels"] == {"Security": 1, "EOL": 0}
    assert out["buildsystems"] == {"meson": 1, "cmake": 0}
    assert all(v == 0 for v in out["counts"].values())


def security_result(aliases=(), kev=None, epss=None, affected=()):
    io = FixtureIO(
        {
            "/v1/query": {"vulns": [{"id": "GHSA-fixture", "aliases": list(aliases), "affected": list(affected)}]},
            "cisa.gov": kev if kev is not None else {"vulnerabilities": []},
            "first.org": epss if epss is not None else {"data": []},
        }
    )
    result = monitor_security.check({"version": "1.2.3"}, {"ecosystem": "PyPI", "name": "fixture"}, io)
    return result, io


def test_security_enrichment_absence_is_not_negative():
    result, io = security_result()
    facts = {f["key"]: f for f in result["findings"][0]["facts"]}
    assert facts["KEV (no CVE alias)"]["status"] == "not_applicable"
    assert len(io.calls) == 1
    result, _ = security_result(["CVE-2026-12345"], kev=ValueError("offline"))
    facts = {f["key"]: f for f in result["findings"][0]["facts"]}
    assert facts["KEV · CVE-2026-12345"]["status"] == "unavailable"
    assert facts["KEV · CVE-2026-12345"]["value"] is None
    assert facts["EPSS · CVE-2026-12345"]["status"] == "unavailable"
    result, _ = security_result(["CVE-2026-12345"])
    facts = {f["key"]: f for f in result["findings"][0]["facts"]}
    assert facts["KEV · CVE-2026-12345"]["status"] == "observed"
    assert facts["KEV · CVE-2026-12345"]["value"] is False


def test_osv_fixed_events_are_scoped_to_query_identity():
    def affected(name, ecosystem, fixed):
        return {
            "package": {"name": name, "ecosystem": ecosystem},
            "ranges": [{"events": [{"introduced": "0"}, {"fixed": fixed}]}],
        }

    result, _ = security_result(
        affected=[
            affected("fixture", "PyPI", "1.3"),
            affected("other", "PyPI", "9.0"),
            affected("fixture", "npm", "8.0"),
        ]
    )
    facts = {f["key"]: f for f in result["findings"][0]["facts"]}
    assert facts["Fixed events"]["value"] == ["1.3"]
    assert facts["Fixed events"]["source"] == "OSV"
    assert facts["Query version"]["value"] == "1.2.3"
    assert facts["Query name"]["value"] == "fixture"


def test_legacy_advice_remains_stored_but_requires_recollection(snapshot):
    old = {
        "id": "old",
        "label": "SecurityReview",
        "title": "old",
        "detail": "Review",
        "evidence_url": "https://example.org/",
        "resolution": "Upgrade",
        "severity": "urgent",
        "tags": [],
        "scope": "current",
        "target_version": None,
    }
    snapshot["monitors"] = {
        "binutils": {
            "security": {
                "subject": monitor_model.subject(snapshot, "binutils"),
                "status": "ok",
                "checked_at": state.utcnow(),
                "findings": [old],
            }
        }
    }
    before = deepcopy(snapshot)
    out = monitor_model.project(snapshot, "binutils", datetime.now(timezone.utc))
    assert out["findings"] == []
    assert out["checks"][0]["status"] == "schema_changed"
    assert snapshot == before


def test_evidence_contract_rejects_advice_fields_and_false_unknowns():
    fact = monitor_model.evidence("KEV", False, "CISA", "https://www.cisa.gov/")
    item = monitor_model.finding("id", "Security", "id", [fact], "https://example.org/")
    for field in ["severity", "resolution", "detail"]:
        with pytest.raises(ValueError):
            monitor_model.validate_findings([{**item, field: "review"}])
    for patch in [{"status": "unavailable"}, {"url": "javascript:alert(1)"}, {"value": float("nan")}]:
        with pytest.raises(ValueError):
            monitor_model.validate_findings([{**item, "facts": [{**fact, **patch}]}])


def test_scanner_findings_do_not_claim_osv_query_or_fixed_events(monkeypatch):
    from tracker import monitor_cve

    monkeypatch.setattr(
        monitor_cve, "scan", lambda subject, settings: [{"id": "CVE-2026-12345", "aliases": [], "affected": []}]
    )
    io = FixtureIO({"cisa.gov": {"vulnerabilities": []}, "first.org": {"data": []}})
    out = monitor_security.check({"version": "5.3"}, {"vendor": "gnu", "product": "bash"}, io)
    facts = out["findings"][0]["facts"]
    assert next(f for f in facts if f["key"] == "Query product")["value"] == "bash"
    assert next(f for f in facts if f["key"] == "Returned advisory")["source"] == "cve-bin-tool"
    assert all(f["key"] != "Fixed events" and f["source"] != "OSV" for f in facts)


def test_unchanged_evidence_preserves_revision_and_changed_time(monkeypatch):
    facts = [monitor_model.evidence('Aliases', ['B', 'A'], 'fixture', 'https://example.org/'),
             monitor_model.evidence('KEV', False, 'fixture', 'https://example.org/')]
    item = monitor_model.finding('id', 'Security', 'id', facts, 'https://example.org/')
    adapter = types.SimpleNamespace(HOSTS=set(), check=lambda *args: {
        'status': 'ok', 'findings': [deepcopy(item)], 'note': 'fixture'})
    monkeypatch.setitem(monitor.REGISTRY, 'fixture', adapter)
    times = iter(['2026-09-20T00:00:00+00:00', '2026-09-20T00:30:00+00:00', '2026-09-20T01:00:00+00:00'])
    monkeypatch.setattr(state, 'utcnow', lambda: next(times))
    proposed = {'status': 'pending', 'subject': {'version': '1'}, 'inputs': {}, 'scope': 'current', 'fingerprint': 'same'}
    first = monitor.execute('fixture', proposed, FixtureIO({}))
    item['facts'].reverse()
    item['facts'][1]['value'].reverse()
    second = monitor.execute('fixture', proposed, FixtureIO({}), first)
    assert second['checked_at'] != first['checked_at']
    assert second['changed_at'] == first['changed_at']
    assert second['evidence_revision'] == first['evidence_revision']
    assert second['findings'] == first['findings']
    item['facts'][0]['value'] = True
    third = monitor.execute('fixture', proposed, FixtureIO({}), second)
    assert third['evidence_revision'] != second['evidence_revision']
    assert third['changed_at'] == third['checked_at']


def test_heartbeat_drains_batches_then_does_not_write(config, snapshot, monkeypatch, tmp_path):
    calls = []
    def check(subject, inputs, io):
        calls.append(subject['name'])
        return {'status': 'ok', 'findings': [], 'note': None}
    monkeypatch.setitem(monitor.REGISTRY, 'fixture', types.SimpleNamespace(
        VERSION=1, HOSTS=set(), inputs=lambda *args: {}, check=check))
    config.update(monitors={'enabled':['fixture'], 'batch_size':1, 'workers':1},
                  config_digest='fixture', nv_digest='fixture')
    monkeypatch.setattr(monitor.cfg, 'load', lambda path: config)
    db=tmp_path/'state.db';state.commit(db,snapshot)
    for _ in snapshot['sources']:
        monitor.collect(config,'unused',db,io=FixtureIO({}))
    assert calls and len(calls)==len(set(calls))
    assert not any(p['fixture']['status']=='pending' for p in state.read(db)['monitors'].values())
    monkeypatch.setattr(state,'commit',lambda *args:pytest.fail('idle heartbeat must not publish'))
    monitor.collect(config,'unused',db,io=FixtureIO({}))


def test_failed_check_uses_attempt_backoff(config, snapshot, monkeypatch, tmp_path):
    calls=[]
    def check(*args):
        calls.append(1)
        raise ValueError('offline')
    monkeypatch.setitem(monitor.REGISTRY,'fixture',types.SimpleNamespace(
        VERSION=1,HOSTS=set(),inputs=lambda *args:{},check=check))
    config.update(monitors={'enabled':['fixture'],'batch_size':100},config_digest='fixture',nv_digest='fixture')
    monkeypatch.setattr(monitor.cfg,'load',lambda path:config)
    db=tmp_path/'state.db';state.commit(db,snapshot)
    monitor.collect(config,'unused',db,io=FixtureIO({}));count=len(calls)
    monitor.collect(config,'unused',db,io=FixtureIO({}))
    assert count>0 and len(calls)==count
