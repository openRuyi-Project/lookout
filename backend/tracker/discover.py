from . import version_rules
# SPDX-FileCopyrightText: (C) 2026 Institute of Software, Chinese Academy of Sciences (ISCAS)
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""Operator-only upstream onboarding. Propose native rules; never mutate live state.

Anitya is an identity/release authority, not a name-to-version oracle. A proposal
requires the SPEC homepage, current version history and a unique project to agree.
The resulting native TOML remains the only tracking authority after review.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
import tomllib
from urllib.parse import urlencode, urlsplit
import httpx

from . import config as cfg, nv, state, discover_sources as sources, version_rules
from .http_io import read_response

API = 'https://release-monitoring.org'
MAX_BODY = 4 * 1024 * 1024
WORKERS = 4
REQUEST_BUDGET = 20


def identity_url(value):
    """Small, offline normalization; never equate sibling projects/subpaths."""
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return None
    try:
        u = urlsplit(value)
        if (u.scheme not in ('http', 'https') or not u.hostname or u.username
                or u.password or u.query or u.fragment or u.port not in (None, 80, 443)):
            return None
    except ValueError:
        return None
    host = u.hostname.lower().removeprefix('www.')
    path = u.path.rstrip('/')
    if '%' in path or any(p in ('.', '..') for p in path.split('/')):
        return None
    if host == 'github.com':
        # /tree/... can mean an independently released monorepo component.
        if not re.fullmatch(r'/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', path):
            return None
        path = path.removesuffix('.git').lower()
    return host + path


def version(value):
    if not isinstance(value, str):
        return None
    value = value.removeprefix('v')
    return value if state.usable_version(value) and re.search(r"[0-9]", value) else None


def candidates(config, snapshot):
    """Only previously unbound packages; explicit operator decisions win."""
    result = []
    urls = Counter(identity_url((v.get('metadata') or {}).get('url'))
                   for v in snapshot.get('specs', {}).values())
    for name, source in sorted(snapshot['sources'].items()):
        binding = cfg.binding(config, name)
        explicit = config.get('packages', {}).get(name, {})
        if binding['compare'] or binding.get('not_applicable') or 'compare' in explicit:
            continue
        spec = snapshot.get('specs', {}).get(name, {})
        metadata = spec.get('metadata') or {}
        url = identity_url(metadata.get('url'))
        reason = None
        if spec.get('error') or not metadata or not spec.get('native_query', {}).get('spec_sha256'):
            reason = 'spec_metadata_unverified'
        elif not url:
            reason = 'homepage_identity_unsupported'
        elif urls[url] > 1:
            # SDL2/SDL3, monorepos, KDE families: a homepage alone cannot pick a line.
            reason = 'shared_homepage_requires_mapping_review'
        elif source.get('error') or source.get('version') != metadata.get('version'):
            reason = 'obs_spec_version_disagrees'
        elif not version(metadata.get('version')):
            reason = 'version_normalization_unsupported'
        result.append({'name': name, 'shared_homepage': bool(url and urls[url] > 1), 'homepage': metadata.get('url'),
                       'current': metadata.get('version'), 'spec_head': spec.get('head'),
                       'spec_sha256': spec.get('native_query', {}).get('spec_sha256'),
                       'reason': reason})
    return result


def shared_release_entries(config, snapshot, rows):
    """Reuse an existing operator rule only for the exact same release directory.

    No family-name registry: pinned source archive identity is the join key;
    disagreeing anchor policies are held, never first-match-selected.
    """
    wanted = {sources.release_directory(r) for r in rows} - {None}
    anchors = {}
    wanted_homepages = {r['homepage'] for r in rows if sources.release_directory(r) in wanted}
    for name, fact in snapshot.get('sources', {}).items():
        track = cfg.binding(config, name)['compare']
        spec = snapshot.get('specs', {}).get(name, {})
        metadata = spec.get('metadata') or {}
        if (not track or spec.get('error') or fact.get('error')
                or metadata.get('version') != fact.get('version')):
            continue
        if metadata.get('url') not in wanted_homepages:
            continue
        anchor = {'name':name, 'homepage':metadata.get('url'), 'current':metadata.get('version'),
                  'spec_sha256':spec.get('native_query',{}).get('spec_sha256')}
        anchor.update(sources.hints(anchor, spec))
        key = sources.release_directory(anchor)
        if key in wanted:
            anchors.setdefault(key, []).append(track)
    for row in rows:
        tracks = sorted(set(anchors.get(sources.release_directory(row),[])))
        policies = {cfg.track_fingerprint(config['native'][t]) for t in tracks}
        if row.get('shared_homepage') and len(policies)==1:
            row.update(reason=None, reuse_track=tracks[0], entry=config['native'][tracks[0]],
                       expected_version=None, identity_evidence='same_versioned_release_directory')


def match(candidate, response):
    """No first-name hit, maximum-number guess, or prerelease fallback."""
    items = response.get('items') if isinstance(response, dict) else None
    if not isinstance(items, list) or response.get('total_items') != len(items):
        return {'reason': 'incomplete_project_search'}
    matched = []
    for item in items:
        if not isinstance(item, dict) or type(item.get('id')) is not int or item['id'] <= 0:
            continue
        candidate_identity = identity_url(candidate['homepage'])
        same_homepage = bool(candidate_identity and identity_url(item.get('homepage')) == candidate_identity)
        source_repository = candidate.get('source_repository')
        upstream_repo = (('https://github.com/' + (item.get('version_url') or ''))
                         if item.get('backend') == 'GitHub' else item.get('version_url'))
        source_matches = bool(identity_url(source_repository) and identity_url(source_repository) in
                              (identity_url(item.get('homepage')), identity_url(upstream_repo)))
        component = candidate.get('archive_component')
        component_matches = bool(component and component.casefold() == str(item.get('name','')).casefold())
        if not (source_matches or (same_homepage and (not candidate.get('shared_homepage') or component_matches))):
            continue
        # A shared forge root cannot identify which compatibility release line.
        if (candidate.get('shared_homepage') or candidate.get('shared_repository')) and (identity_url(source_repository) or candidate_identity or '').startswith('github.com/'):
            continue
        if not isinstance(item.get('versions'), list) or not isinstance(item.get('stable_versions'), list):
            continue
        # Prove version vocabulary/line against the currently packaged version.
        if version(candidate['current']) not in {version(v) for v in item['versions']}:
            continue
        stable = item['stable_versions']
        if not stable or not version(stable[0]):
            continue
        matched.append(item)
    if len(matched) != 1:
        return {'reason': 'no_unique_identity_and_version_match', 'matching_ids': [x['id'] for x in matched],
                'url_candidates': [{'id': x.get('id'), 'name': x.get('name'), 'homepage': x.get('homepage')}
                                   for x in items if isinstance(x, dict)]}
    project = matched[0]
    return {'reason': None, 'project_id': project['id'], 'project_name': project.get('name'),
            'identity_evidence': 'source_or_homepage_history',
            'project_homepage': project['homepage'], 'expected_version': version(project['stable_versions'][0]),
            'entry': {'source': 'jq', 'url': f'{API}/api/v2/versions/?project_id={project["id"]}',
                      'filter': 'first(.stable_versions[])', 'prefix': 'v'}}


def fetch_project(name, client):
    """Only the fixed public API is contacted; SPEC URLs are never fetched."""
    url = API + '/api/v2/projects/?' + urlencode({'name': name, 'items_per_page': 250})
    record = {'url': url, 'at': state.utcnow()}
    try:
        deadline = time.monotonic() + REQUEST_BUDGET
        with client.stream('GET', url, headers={'Accept': 'application/json'}) as response:
            record['http_status'] = response.status_code
            response.raise_for_status()
            body = read_response(response, max_bytes=MAX_BODY, deadline=deadline)
        record['body_sha256'] = hashlib.sha256(body).hexdigest()
        record['body'] = json.loads(body)
    except (httpx.HTTPError, ValueError) as error:
        record['error'] = type(error).__name__  # no credential-bearing exception strings
    return record


def append_entries(text, entries):
    raw = tomllib.loads(text)
    original = raw
    if set(original) & set(entries):
        raise ValueError('discovery must never overwrite an existing native track')
    addition = '\n' + nv.dump_config(dict(sorted(entries.items()))) if entries else ''
    output = text + ('' if text.endswith('\n') else '\n') + addition
    if not version_rules.same_values(tomllib.loads(output), {**original, **entries}):
        raise ValueError('discovery output differs from the requested native rules')
    return output


def verify(config, proposals):
    """Reuse the actual native collector, including fingerprints and failures."""
    if not proposals:
        return {}, None
    text = Path(config['nvpath']).read_text()
    if version_rules.digest(config['nvpath']) != config['nv_digest']:
        raise ValueError('native configuration changed during discovery')
    entries = {p['name']: p['entry'] for p in proposals}
    candidate = {**config, 'native': {**config['native'], **entries}}
    facts, error = nv.run(candidate, {}, state.utcnow(), tracks=list(entries))
    return facts, error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--db', required=True, help='Read-only tracker snapshot, preferably a SQLite backup')
    parser.add_argument('--output', required=True, help='New evidence directory; never a runtime config directory')
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--go-proxy-url', default='https://proxy.golang.org', help='Operator-selected Go module proxy; emitted rules retain this origin')
    parser.add_argument('--evidence-cache', type=Path, help='Reuse matching saved project searches; native verification still runs live')
    parser.add_argument('--github-limit', type=int, default=40, help='Bound native GitHub latest-release fallback; 0 disables it')
    parser.add_argument('--verify', action='store_true', help='Run native nvchecker before emitting a candidate config')
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 2000:
        parser.error('--limit must be in 1..2000')
    snapshot = state.read(args.db)
    config = cfg.load(args.config)
    output = Path(args.output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    if not 0 <= args.github_limit <= 2000:
        parser.error('--github-limit must be in 0..2000')
    rows = candidates(config, snapshot)
    for row in rows:
        if row['reason'] in (None, 'shared_homepage_requires_mapping_review', 'homepage_identity_unsupported'):
            row.update(sources.hints(row, snapshot.get('specs', {}).get(row['name'], {})))
            if row.get('go_module'):
                row.update(reason=None, entry=sources.go_entry(row['go_module'], args.go_proxy_url),
                           expected_version=None, identity_evidence='native_go_module',
                           comparable=not bool(re.search(r'git|~|\^', str(row['current']))))
            elif row['reason'] == 'shared_homepage_requires_mapping_review' and row.get('archive_component'):
                row['reason'] = None
            elif row['reason'] == 'homepage_identity_unsupported' and row.get('source_repository'):
                row['reason'] = None
    repo_packages = {}
    for name, spec in snapshot.get('specs', {}).items():
        identity = identity_url((spec.get('metadata') or {}).get('url'))
        if identity:
            repo_packages.setdefault(identity, set()).add(name)
    for row in rows:
        identity = identity_url(row.get('source_repository'))
        if identity:
            repo_packages.setdefault(identity, set()).add(row['name'])
    for row in rows:
        identity = identity_url(row.get('source_repository') or row['homepage'])
        row['shared_repository'] = len(repo_packages.get(identity, ())) > 1
    shared_release_entries(config, snapshot, rows)
    # Spend the bounded verification budget on candidates with a concrete
    # identity first.  This is data-driven: repository/component/release
    # evidence comes from SPEC and provider responses, never package names.
    eligible = [r for r in rows if not r['reason']]
    eligible.sort(key=lambda r: (
        not bool(r.get('entry')),
        not bool(r.get('reuse_track')),
        not bool(r.get('source_repository')),
        not bool(r.get('archive_component')),
        r['name'],
    ))
    selected = eligible[:args.limit]
    selected_names = {r['name'] for r in selected}
    for row in rows:
        if not row['reason'] and row['name'] not in selected_names:
            row['reason'] = 'not_attempted_limit'
    def discover(row):
        if row.get('entry'):
            return row
        query = row.get('archive_component') or row['name']
        evidence = None
        if args.evidence_cache:
            cache = args.evidence_cache / (hashlib.sha256(row['name'].encode()).hexdigest()+'.json')
            if cache.is_file():
                saved = json.loads(cache.read_text())
                expected_url = API + '/api/v2/projects/?' + urlencode({'name':query, 'items_per_page':250})
                if saved.get('url') == expected_url and 'body' in saved and not saved.get('error'):
                    evidence = saved
        if evidence is None:
            evidence = fetch_project(query, client)
        (output / (hashlib.sha256(row['name'].encode()).hexdigest() + '.json')).write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
        return {**row, **({'reason': 'discovery_request_failed'} if evidence.get('error')
                         else match(row, evidence['body']))}
    timeout = httpx.Timeout(connect=20, read=20, write=20, pool=20)
    limits = httpx.Limits(max_connections=WORKERS, max_keepalive_connections=WORKERS)
    with httpx.Client(timeout=timeout, limits=limits, follow_redirects=False) as client:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            updates = {r['name']: r for r in pool.map(discover, selected, buffersize=WORKERS)}
    rows = [updates.get(r['name'], r) for r in rows]
    github_count = 0
    for row in rows:
        if row['reason'] != 'no_unique_identity_and_version_match' or row.get('shared_homepage') or row.get('shared_repository'):
            continue
        repo = row.get('source_repository') or row['homepage']
        identity = identity_url(repo)
        if identity and identity.startswith('github.com/') and github_count < args.github_limit:
            github_count += 1
            row.update(reason=None, entry={'source':'github', 'github':identity[len('github.com/'):],
                       'use_latest_release':True, 'prefix':'v'}, expected_version=None,
                       identity_evidence='source_repository_release' if row.get('source_repository') else 'homepage_repository_release',
                       comparable=not bool(re.search(r'git|~|\^', str(row['current']))))
    proposals = [r for r in rows if not r['reason']]
    observations, command_error = verify(config, proposals) if args.verify else ({}, None)
    accepted = {}
    bindings = {}
    for row in proposals:
        fact = observations.get(row['name'], {})
        row['verification'] = fact
        if not args.verify:
            row['reason'] = 'native_verification_not_requested'
        elif (fact.get('error') or not fact.get('fetched_at')
              or not version(fact.get('version'))
              or (row['expected_version'] is not None and fact.get('version') != row['expected_version'])
              or fact.get('configuration_fingerprint') != cfg.track_fingerprint(row['entry'])):
            row['reason'] = 'native_verification_failed_or_provider_changed'
        else:
            row['reason'] = 'verified'
            if row.get('reuse_track'):
                bindings[row['name']] = {'compare':row['reuse_track']}
            else:
                accepted[row['name']] = row['entry']
            if row.get('comparable') is False:
                bindings.setdefault(row['name'], {})['comparable'] = False
    text = Path(config['nvpath']).read_text()
    if version_rules.digest(config['nvpath']) != config['nv_digest']:
        raise ValueError('native configuration changed during discovery')
    if args.verify:
        (output / 'candidate.nvchecker.toml').write_text(append_entries(text, accepted))
    (output / 'candidate-bindings.json').write_text(json.dumps(bindings,indent=2)+'\n')
    # URL mismatch is review evidence, not proof that either URL is wrong.
    for row in rows:
        if row.get('project_homepage') and identity_url(row['homepage']) != identity_url(row['project_homepage']):
            row['url_review'] = {'status': 'corroborated_by_source' if row['reason']=='verified' else 'needs_review',
                                 'spec_homepage':row['homepage'], 'provider_homepage':row['project_homepage']}
    report = {'schema': 2, 'at': datetime.now(timezone.utc).isoformat(),
              'snapshot_generation': snapshot['generation'], 'native_sha256': config['nv_digest'],
              'untracked_considered': len(rows), 'queried': len(selected), 'verified': sum(r['reason']=='verified' for r in rows), 'new_native_rules':len(accepted), 'new_bindings':len(bindings),
              'command_error': command_error, 'reasons': dict(Counter(r['reason'] for r in rows)),
              'packages': rows, 'live_state_modified': False}
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'packages'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
