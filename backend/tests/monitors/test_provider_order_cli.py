"""Production jq rules, real nvchecker CLI, and only the HTTP transport replaced.

Anitya's stable_versions is already descending in the project's own scheme.
The saved project270 response is an actual historical-ordering regression, not a
hand-written runtime version override. Other inputs exercise that same contract.
"""
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading
import tomllib

import pytest

from tracker.config import track_fingerprint
from tracker.monitors.version.nvchecker import import_events

ROOT = Path(__file__).resolve().parents[3]
NATIVE = __import__('tracker.monitors.version.rules',fromlist=['expand']).load((ROOT / 'config/versions/nvchecker.toml')).entries
NOW = '2026-09-20T00:00:00+00:00'


@pytest.fixture
def native_check(tmp_path):
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def run(entry, payload, expected, previous=None):
        (tmp_path / 'versions.json').write_text(payload if isinstance(payload, str) else json.dumps(payload))
        transport = {**entry, 'url': f'http://127.0.0.1:{server.server_port}/versions.json'}
        path = tmp_path / 'native.toml'
        path.write_text('[fixture]\n' + '\n'.join(k + ' = ' + json.dumps(v)
                                                 for k, v in transport.items()) + '\n')
        command = ['nvchecker', '--logger=json', '--json-log-fd=1', '--failures', '-c', str(path)]
        process = subprocess.run(command, capture_output=True, text=True, timeout=30)
        print('PROVIDER_ORDER:', json.dumps(dict(command=command, stdout=process.stdout,
                                                stderr=process.stderr, exit_status=process.returncode)))
        assert process.returncode == (0 if expected is not None else 3)
        # Keep the real production identity when importing transport-only fixtures.
        facts, error = import_events(process.stdout, {'fixture': entry}, previous or {}, NOW)
        fact = facts['fixture']
        if expected is not None:
            assert fact['version'] == expected and not fact.get('error') and error is None
            assert fact['configuration_fingerprint'] == track_fingerprint(entry)
        else:
            assert fact['error']
        return fact

    try:
        yield run
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_real_history_baseline_modified_and_rule_rollback(native_check):
    payload = json.loads((Path(__file__).parents[1] / 'fixtures/anitya-ordered-history.json').read_text())
    fixed = NATIVE['cfitsio']
    baseline = {**fixed, 'filter': '.stable_versions[]'}
    native_check(baseline, payload, '3100')
    native_check(fixed, payload, '4.7.0')
    native_check(baseline, payload, '3100')


@pytest.mark.parametrize('payload, expected', [
    ({'stable_versions': ['4.8.0', '4.7.0', '3100', 'latest']}, '4.8.0'),
    ({'latest_version': '5.0.0rc1', 'versions': ['5.0.0rc1', '4.8.0'],
      'stable_versions': ['4.8.0', '4.7.0']}, '4.8.0'),
    ({'stable_versions': ['2026.09', '2026.08']}, '2026.09'),
    ({'stable_versions': ['R2', 'R1']}, 'R2'),
    ({'stable_versions': ['v2.1', 'v2.0']}, '2.1'),
    ({'stable_versions': []}, None),
    ({'versions': ['5.0rc1']}, None),
    ({'stable_versions': None}, None),
])
def test_provider_contract_without_fixed_version_or_global_format(native_check, payload, expected):
    native_check(NATIVE['cfitsio'], payload, expected)


@pytest.mark.parametrize('name, versions, expected', [
    ('expat', ['v3.0.0rc1', 'v2.7.1', 'v2.7.0'], '2.7.1'),
    ('gcc16', ['v17.1.0', 'v16.2.0', 'v16.1.0', 'v15.3.0'], '16.2.0'),
    ('libiberty', ['v17.1.0', 'v16.2.0', 'v16.1.0', 'v15.3.0'], '16.2.0'),
    ('openssl', ['v4.0.0', 'v3.6.1', 'v3.6.0', 'v1.1.1w'], '3.6.1'),
    ('wlroots-0.19', ['v0.20.0', 'v0.19.2', 'v0.19.1', 'v0.18.2'], '0.19.2'),
    ('gcc16', ['v17.1.0', 'v15.3.0'], None),
    ('openssl', ['v4.0.0'], None),
    ('expat', ['v2.8.0\n', 'v2.7.1'], '2.7.1'),
    ('gcc16', ['v16.3.0\n', 'v16.2.0'], '16.2.0'),
    ('libiberty', ['v16.3.0\n', 'v16.2.0'], '16.2.0'),
    ('openssl', ['v3.7.0\n', 'v3.6.1'], '3.6.1'),
    ('wlroots-0.19', ['v0.19.3\n', 'v0.19.2'], '0.19.2'),
])
def test_maintenance_filter_before_provider_first(native_check, name, versions, expected):
    assert 'include_regex' not in NATIVE[name], 'One authoring point: the predicate is before jq first'
    native_check(NATIVE[name], {'stable_versions': versions}, expected)


def test_failed_new_rule_never_relabels_old_3100(native_check):
    fixed = NATIVE['cfitsio']
    baseline = {**fixed, 'filter': '.stable_versions[]'}
    previous = {'fixture': {'version': '3100', 'fetched_at': '2026-09-19T00:00:00+00:00',
                            'configuration_fingerprint': track_fingerprint(baseline)}}
    fact = native_check(fixed, {'stable_versions': []}, None, previous)
    assert 'version' not in fact and 'fetched_at' not in fact
    assert fact['previous_configuration']['version'] == '3100'
    assert fact['configuration_fingerprint'] == track_fingerprint(fixed)


def test_selection_config_is_not_rewritten_by_new_release(native_check):
    path = ROOT / 'config/versions/nvchecker.toml'
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    for version in ['4.8.0', '4.9.0']:
        native_check(NATIVE['cfitsio'], {'stable_versions': [version, '3100']}, version)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize('versions, expected', [
    (['2_37', '2.37', '2.36'], '2.37'),
    (['2_38', '2_37', '2.37'], '2.38'),
    (['3_0', '2_99'], '3.0'),
])
def test_official_tag_representation_normalized_without_fixed_version(native_check, versions, expected):
    native_check(NATIVE['fonts-dejavu'], {'stable_versions': versions}, expected)


def test_whois_official_source_manifests_not_arbitrary_archives(native_check):
    index = (Path(__file__).parents[1] / 'fixtures/whois-releases.html').read_text()
    assert 'whois_5.6.6.git.tar.xz' in index and 'whois_5.6.6.dsc' in index
    native_check(NATIVE['whois'], index, '5.6.6')


def test_whois_next_manifest_not_pinned_and_noise_excluded(native_check):
    index = '\n'.join(f'<a href="{name}">{name}</a>' for name in [
        'whois_5.7.dsc', 'whois_5.6.7.dsc', 'whois_5.6.6.dsc',
        'whois_6.0rc1.dsc', 'whois_99.0.git.tar.xz', 'whois_99.0.tar.xz',
        'whois_99.0+deb13u1.dsc', 'unrelated_99.0.dsc'])
    native_check(NATIVE['whois'], index, '5.7')


def test_whois_no_manifest_is_unknown_not_archive_version(native_check):
    native_check(NATIVE['whois'], '<a href="whois_99.0.git.tar.xz">archive</a>', None)


@pytest.mark.parametrize('releases, expected', [
    ([{'release_number': '91.2.3', 'status': 'stable'},
      {'release_number': '92.0.1', 'status': 'rc'},
      {'release_number': '93.0.0', 'status': 'preview'}], '91.2.3'),
    ([{'release_number': '93.0.0', 'status': 'preview'}], None),
])
def test_mariadb_uses_publisher_release_status(native_check, releases, expected):
    native_check(NATIVE['mariadb'], {'releases': releases}, expected)


def test_dconf_accepts_new_numbering_without_accepting_preview(native_check):
    versions = {v: {'tar.xz': 'fixture.tar.xz'}
                for v in ['0.49.0', '96.0', '97.0', '98.beta', '98.rc']}
    native_check(NATIVE['dconf'], [None, {'dconf': versions}], '97.0')


def test_rpm_release_page_keeps_component_and_channel(native_check):
    native_check(NATIVE['rpm'],
                 '<a>RPM 91.2.3</a><a>RPM 92.0.0-rc1</a><a>POPT 99.0.0</a>', '91.2.3')


def test_lame_ignores_other_sourceforge_components_and_oldfiles(native_check):
    native_check(NATIVE['lame'], '\n'.join([
        '/lame/91.2/lame-91.2.tar.gz',
        '/lame/92.0/lame-92.0beta.tar.gz',
        '/OldFiles/lame-99.0.tar.gz',
        '/py-lame/99.0/py-lame-99.0.tar.gz',
    ]), '91.2')


def test_rust_toolchain_manifest_selects_rust_not_bundled_tools(native_check):
    native_check(NATIVE['rust-bin'],
                 '[pkg.rust]\nversion = "91.2.3 (fixture)"\n'
                 '[pkg.cargo]\nversion = "99.0.0 (fixture)"\n', '91.2.3')
