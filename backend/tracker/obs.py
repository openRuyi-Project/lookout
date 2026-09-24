"""Consume structured OBS facts; never evaluate a spec file."""
from defusedxml import ElementTree as ET
import httpx
import time
import re
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlsplit
from .state import usable_version
from .schedule import Schedule
from .http_io import read_response

MAX_XML = 20 * 1024 * 1024


def polling(config):
    """Keep status polling cheap; source/history requests use their separate lane."""
    options = config['collector']
    build = options['build_interval_seconds']
    metadata = options['obs_interval_seconds']
    return {'builds': Schedule(build, build * 2, max(build * 2, 300)),
            'obs-metadata': Schedule(metadata, metadata * 2, max(metadata * 2, 900))}

def xml(data, root_tag):
    if len(data) > MAX_XML:
        raise ValueError('OBS response too large')
    root = ET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    if root.tag != root_tag:
        raise ValueError('unexpected OBS XML root')
    return root

def inventory(data):
    root = xml(data, 'directory')
    rows = {}
    for e in root:
        name = e.get('name')
        if e.tag != 'entry' or not name or name in rows or '/' in name:
            raise ValueError('invalid or duplicate inventory entry')
        rows[name] = e.get('originpackage') or name
    if any(owner not in rows for owner in rows.values()):
        raise ValueError('flavor owner absent from inventory')
    return rows

def source_index(data):
    root = xml(data, 'sourceinfolist')
    result = {}
    for e in root:
        name = e.get('package')
        if e.tag != 'sourceinfo' or not name or name in result:
            raise ValueError('invalid or duplicate source index')
        result[name] = {'rev': e.get('rev'), 'srcmd5': e.get('srcmd5')}
    return result

def source_info(data, name, expected_hash=None):
    root = xml(data, 'sourceinfo')
    if root.get('package') != name:
        raise ValueError('source package mismatch')
    if expected_hash and root.get('srcmd5') != expected_hash:
        raise ValueError('source revision changed during collection')
    version = root.findtext('version')
    if not version or version in ('unknown', 'None') or len(version) > 200:
        raise ValueError('OBS source version unavailable')
    return {'version': version if usable_version(version) else None, 'raw_version': version,
            'version_error': None if usable_version(version) else 'OBS source version contains an unresolved or non-VERSION value',
            'rev': root.get('rev'), 'srcmd5': root.get('srcmd5'),
            'filename': root.findtext('filename'), 'version_basis': 'OBS sourceinfo/version'}

def validate_targets(data, project, targets):
    root = xml(data, 'project')
    if root.get('name') != project:
        raise ValueError('OBS project mismatch')
    actual = {(r.get('name'), a.text) for r in root.findall('repository') for a in r.findall('arch')}
    if not all((t['repository'], t['architecture']) in actual for t in targets):
        raise ValueError('configured target absent in OBS metadata')

def build_results(data, project, targets):
    root = xml(data, 'resultlist')
    wanted = {(t['repository'], t['architecture']): t['id'] for t in targets}
    result = {}
    for r in root.findall('result'):
        key = (r.get('repository'), r.get('arch'))
        if r.get('project') != project or key not in wanted:
            continue
        target = wanted[key]
        if target in result:
            raise ValueError('duplicate OBS result target')
        packages = {}
        for e in r.findall('status'):
            name, code = e.get('package'), e.get('code')
            if not name or not code or name in packages:
                raise ValueError('invalid or duplicate build status')
            packages[name] = {'raw_status': code, 'details': (e.findtext('details') or '')[:4000], 'matches_source': None}
        result[target] = packages
    return result

def last_successes(data):
    """OBS lastfailures includes last success/reuse and subsequent failures.

    One response covers a whole target. Do not expose worker URIs or treat a
    failed job's version as a successful build. An unchanged/reused job is not
    a new successful build, so it cannot supply a new success time. endtime is completion, not queue
    time. versrel is VERSION-RELEASE; only VERSION is compared with upstream.
    """
    root = xml(data, 'jobhistlist')
    result, reused = {}, set()
    for entry in root:
        if entry.tag != 'jobhist' or not entry.get('package'):
            raise ValueError('invalid OBS job history entry')
        if entry.get('code') == 'unchanged':
            reused.add(entry.get('package'))
        if entry.get('code') != 'succeeded':
            continue
        name = entry.get('package')
        versrel = entry.get('versrel', '')
        version, separator, release = versrel.rpartition('-')
        if not separator or not release or not usable_version(version):
            version = None  # OBS's parser can leave MACRO/% placeholders even after success.
        try:
            epoch = int(entry.get('endtime', ''))
            if epoch <= 0:
                raise ValueError('invalid build completion time')
            stamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='seconds')
        except (ValueError, OverflowError, OSError) as e:
            raise ValueError('invalid build completion time') from e
        fact = {'version': version, 'time': stamp, 'srcmd5': entry.get('srcmd5')}
        if entry.get('rev') and entry.get('bcnt'):
            fact['build_identity'] = {'rev': entry.get('rev'), 'bcnt': entry.get('bcnt'), 'versrel': versrel}
        if name not in result or stamp > result[name]['time']:
            result[name] = fact
    # OBS hides the earlier actual success behind its last unchanged record.
    # None means incomplete history, not evidence that no build ever succeeded.
    for name in reused:
        result.setdefault(name, None)
    return result

def success_identity(fact):
    """The same source may be rebuilt; its hash alone is not a build identity."""
    build = fact.get('build_identity') or {}
    return (fact.get('srcmd5'), fact.get('time'), build.get('rev'), build.get('bcnt'), build.get('versrel'))


def success_binary(data, architecture):
    """Choose one source RPM, or a sole target binary; never guess a subpackage."""
    root = xml(data, 'binaryversionlist')
    names = []
    for entry in root:
        name = entry.get('name')
        if entry.tag != 'binary' or not name or '/' in name or name in names:
            raise ValueError('invalid binary inventory')
        names.append(name)
    sources = [name for name in names if name.endswith(('.src.rpm', '.nosrc.rpm'))]
    candidates = sources or [name for name in names if name.endswith('.' + architecture + '.rpm')
                            and '-debuginfo-' not in name and '-debugsource-' not in name]
    if len(candidates) != 1:
        raise ValueError('successful build RPM is ambiguous or missing')
    return candidates[0]


def binary_success_version(data, filename, project, target, package, success):
    """Expanded RPM header VERSION, proven to belong to this historical build.

    disturl binds project/repository/source hash/flavor. RELEASE additionally
    binds the OBS source release + build counter, so a later rebuild of the same
    source cannot lend its version to an older success. Ambiguity stays unknown.
    """
    root = xml(data, 'fileinfo')
    if root.get('filename') != filename:
        raise ValueError('binary filename mismatch')
    version, release, arch = (root.findtext(k) for k in ('version', 'release', 'arch'))
    if not usable_version(version) or arch not in ('src', 'nosrc', 'noarch', target['architecture']):
        raise ValueError('binary VERSION or architecture unavailable')
    build = success.get('build_identity') or {}
    raw_release = build.get('versrel', '').rpartition('-')[2]
    count = build.get('bcnt') or ''
    if (not build.get('rev') or not re.fullmatch(r'[0-9]+', count)
            or not usable_version(raw_release)):
        raise ValueError('historical build identity unavailable')
    expected_release = raw_release + '.' + count
    if not release or not (release == expected_release or release.startswith(expected_release + '.')):
        raise ValueError('binary build counter/release mismatch')
    # Artifact mtime is publication/signing metadata, not build completion.
    # It cannot invalidate the source/release/build-counter identity above.
    try:
        artifact_time = int(root.findtext('mtime') or '')
        if artifact_time <= 0:
            artifact_time = None
    except (ValueError, TypeError):
        artifact_time = None
    disturl = urlsplit(root.findtext('disturl') or '')
    expected = '/' + '/'.join((project, target['repository'], (success.get('srcmd5') or '') + '-' + package))
    if disturl.scheme != 'obs' or unquote(disturl.path) != expected or not success.get('srcmd5'):
        raise ValueError('binary historical source identity mismatch')
    return {'version': version, 'version_basis': 'OBS RPM fileinfo/version',
            'binary_provenance': {'filename': filename, 'release': release, 'architecture': arch,
                                  'repository': target['repository'], 'target_architecture': target['architecture'],
                                  'srcmd5': success['srcmd5'], 'mtime': artifact_time}}


def resolve_success_version(client, project, target, package, success):
    # Guard missing identity before issuing either of the two bounded requests.
    if not all(success_identity(success)):
        raise ValueError('historical build identity unavailable')
    path = '/build/' + '/'.join(quote(part, safe='') for part in
                               (project, target['repository'], target['architecture'], package))
    filename = success_binary(client.get(path + '?view=binaryversions'), target['architecture'])
    data = client.get(path + '/' + quote(filename, safe='') + '?view=fileinfo')
    return binary_success_version(data, filename, project, target, package, success)

class Client:
    def __init__(self, config, *, attempts=3):
        self.attempts = attempts
        self.base = config['obs']['api_url'].rstrip('/')
        self.project = quote(config['obs']['project'], safe='')
        self.timeout = config['collector'].get('timeout_seconds', 20)
        workers = config['collector'].get('source_workers', 4)
        self.client = httpx.Client(
            timeout=httpx.Timeout(connect=self.timeout, read=self.timeout, write=self.timeout, pool=self.timeout),
            limits=httpx.Limits(max_connections=workers, max_keepalive_connections=workers),
            follow_redirects=False, headers={'User-Agent': 'openruyi-tracker/0.1'})
    def close(self):
        self.client.close()
    def get(self, path):
        for attempt in range(self.attempts):
            try:
                deadline = time.monotonic() + self.timeout
                with self.client.stream('GET', self.base + path) as r:
                    r.raise_for_status()
                    return read_response(r, max_bytes=MAX_XML, deadline=deadline)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as e:
                # A missing/forbidden source will not recover from three immediate
                # identical requests. Keep the error for the next scheduled poll.
                permanent = (isinstance(e, httpx.HTTPStatusError)
                             and e.response.status_code < 500
                             and e.response.status_code not in (408, 429))
                if permanent or attempt == self.attempts - 1:
                    raise
                time.sleep(0.25 * (2 ** attempt))
        raise AssertionError('unreachable')
