"""Security candidates, not a claim that distribution backports are absent."""

import json
import re
from collections import defaultdict
from datetime import date
from ipaddress import ip_address
from urllib.parse import quote, urlsplit

from tracker.monitors.issues import Issue
from tracker.monitors.model import evidence, finding
from tracker.monitors.model import version_query as query_subject
from tracker.monitors.schedule import Schedule
from tracker.monitors.security import nvd
from tracker.monitors.source.release import commit_hash, tag_matches_version

__all__ = ['TITLE', 'VERSION', 'HOSTS', 'inputs', 'check', 'refresh', 'query_subject', 'prepare', 'check_prepared']


TITLE = Issue.ADVISORY
VERSION = 9
HOSTS = {"api.osv.dev", "services.nvd.nist.gov", "www.cisa.gov", "api.first.org"}
CVE = re.compile(r"CVE-\d{4}-\d{4,}")


def refresh(subject, inputs, previous):
    # New advisories/KEV entries can arrive without any source-version change.
    retry = 900 if inputs and inputs.get('source') == 'nvd' else 300
    return Schedule(interval_seconds=21600, retry_seconds=retry)


def inputs(package, configured):
    if configured is not None and 'vendor' in configured:
        return {'source': 'nvd', **configured}
    from tracker.identity import from_package
    identity = configured if configured is not None else from_package(package)
    if identity and 'commit' in identity:
        if configured is not None:
            pinned = package.get('source_commit')
            repository = str(identity.get('repository', '')).rstrip('/').removesuffix('.git')
            if (not pinned or identity['commit'] != pinned['commit']
                    or repository != pinned['repository'].rstrip('/').removesuffix('.git')):
                raise ValueError('Configured security commit does not match confined Source0 evidence')
        return identity
    if identity and identity.get('ecosystem') in ('PyPI', 'crates.io', 'Go', 'npm', 'GIT'):
        if query_version(package.get('version'), identity['ecosystem']):
            return identity
        return package.get('source_commit') or identity
    if configured is not None:
        return configured
    if package.get('source_commit'):
        return package['source_commit']
    tagged = package.get('source_tag')
    if tagged and public_reference(tagged['repository']):
        return {'ecosystem': 'GIT', 'name': tagged['repository'], 'tag': tagged['tag']}
    return None


def query_version(version, ecosystem):
    if ecosystem == 'PyPI':
        from packaging.version import InvalidVersion, Version
        try:
            if Version(version).local is not None:
                return False
        except (InvalidVersion, TypeError):
            return False
    if ecosystem in ('Go', 'crates.io'):
        return isinstance(version, str) and bool(re.fullmatch(
            r'v?[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?', version))
    return isinstance(version, str) and bool(version)


def query(subject, settings):
    if set(settings) == {'repository', 'commit'}:
        if not commit_hash(settings['commit']) or not public_reference(settings['repository']):
            raise ValueError('security requires a corroborated full commit and public repository')
        return {'commit': settings['commit']}
    if (set(settings) - {'ecosystem', 'name', 'tag'} or not {'ecosystem', 'name'} <= set(settings)
            or settings['ecosystem'] not in ('PyPI', 'crates.io', 'Go', 'npm', 'GIT')):
        raise ValueError('security requires an explicit supported ecosystem/name')
    if settings['ecosystem'] == 'GIT' and not public_reference(settings['name']):
        raise ValueError('security requires a public repository URL')
    if 'tag' in settings and settings['ecosystem'] != 'GIT':
        raise ValueError('release tags require a GIT repository identity')
    if 'tag' in settings and not tag_matches_version(settings['tag'], subject['version']):
        return None
    version = settings.get('tag', subject['version'])
    if not query_version(version, settings['ecosystem']):
        return None
    return {'package': {key: settings[key] for key in ('ecosystem', 'name')}, 'version': version}


def osv_page(response):
    if not isinstance(response, dict) or response.keys() - {'vulns', 'next_page_token'}:
        raise ValueError('Invalid OSV query result')
    rows, token = response.get('vulns', []), response.get('next_page_token')
    if not isinstance(rows, list) or token is not None and not isinstance(token, str):
        raise ValueError('Invalid OSV pagination')
    return rows, token


def osv(subject, settings, io):
    request = query(subject, settings)
    if request is None:
        return None
    entries, token, seen = [], None, set()
    for _ in range(20):
        response = io.json('POST', 'https://api.osv.dev/v1/query', {**request, **({'page_token': token} if token else {})})
        rows, token = osv_page(response)
        for item in rows:
            if 'affected' not in item:
                item = io.json('GET', 'https://api.osv.dev/v1/vulns/' + quote(item['id'], safe=''))
            if not item.get('withdrawn'):
                entries.append(item)
        if not token:
            return entries
        if token in seen:
            break
        seen.add(token)
    raise ValueError('OSV pagination exceeds budget')



OSV_BATCH_SIZE = 100
OSV_URL = 'https://api.osv.dev/v1/'


def osv_batch(requests, io, detail):
    """Keep pagination and errors attached to the original query index."""
    entries, seen = [[] for _ in requests], [set() for _ in requests]
    errors = {}
    pending = dict(enumerate(requests))
    for _ in range(20):
        try:
            response = io.json('POST', OSV_URL + 'querybatch', {'queries': list(pending.values())})
            results = response['results']
            if not isinstance(results, list) or len(results) != len(pending):
                raise ValueError('OSV batch response does not match the queries')
        except Exception as error:
            for index in pending:
                errors[index] = error
            break
        next_page = {}
        for index, result in zip(pending, results, strict=True):
            try:
                rows, token = osv_page(result)
                for row in rows:
                    identity, modified = row['id'], row['modified']
                    if not isinstance(identity, str) or not identity or not isinstance(modified, str) or not modified:
                        raise ValueError('Invalid OSV advisory identity')
                    item = detail(identity, modified)
                    if not item.get('withdrawn'):
                        entries[index].append(item)
                if token:
                    if token in seen[index]:
                        raise ValueError('Repeated OSV page token')
                    seen[index].add(token)
                    next_page[index] = {**requests[index], 'page_token': token}
            except Exception as error:
                errors[index] = error
        pending = next_page
        if not pending:
            break
    else:
        for index in pending:
            errors[index] = ValueError('OSV batch pagination exceeds budget')
    return [errors.get(index, rows) for index, rows in enumerate(entries)]


def prepare(jobs, owner):
    """Batch only due OSV queries that share the same cache-age policy."""
    by_age = defaultdict(lambda: defaultdict(list))
    jobs_by_age = defaultdict(list)
    for name, proposed, previous, policy in jobs:
        settings, subject = proposed['inputs'], proposed['subject']
        age = min(policy.interval_seconds, policy.retry_seconds) if previous.get('status') in (
            'error', 'partial') else policy.interval_seconds
        jobs_by_age[age].append((name, subject, settings))
        if 'vendor' in settings:
            continue
        try:
            request = query(subject, settings)
        except (ValueError, KeyError, TypeError):
            continue  # execute() records invalid individual inputs.
        if request is not None:
            by_age[age][json.dumps(request, sort_keys=True)].append(name)
    prepared = {}
    for age, identities in by_age.items():
        keys = list(identities)
        # A single identity costs one query without detail fan-out.
        if len(keys) < 2:
            continue
        details = {}
        for start in range(0, len(keys), OSV_BATCH_SIZE):
            chunk = keys[start:start + OSV_BATCH_SIZE]
            scoped = owner.for_hosts(HOSTS, max_age=age)

            def detail(identity, modified):
                key = (identity, modified)
                if key not in details:
                    item = scoped.json('GET', OSV_URL + 'vulns/' + quote(identity, safe=''),
                                       cache_version=modified)
                    if item.get('id') != identity or item.get('modified') != modified:
                        raise ValueError('OSV detail does not match queried advisory revision')
                    details[key] = item, tuple(scoped.observed[-1:])
                item, times = details[key]
                scoped.observed.extend(times)
                return item

            results = osv_batch([json.loads(key) for key in chunk], scoped, detail)
            for key, result in zip(chunk, results, strict=True):
                for name in identities[key]:
                    prepared[name] = (result, tuple(scoped.observed))
    for age, items in jobs_by_age.items():
        if len(items) < 2:
            continue
        cves, package_cves = set(), {}
        for name, subject, settings in items:
            if name not in prepared:
                scoped = owner.for_hosts(HOSTS, max_age=age)
                try:
                    entries = load_entries(subject, settings, scoped)
                except Exception as error:
                    entries = error
                prepared[name] = (entries, tuple(scoped.observed))
            entries, observed = prepared[name]
            if isinstance(entries, list):
                try:
                    package_cves[name] = cve_ids(entries)
                except (KeyError, TypeError, ValueError) as error:
                    prepared[name] = (error, observed)
                else:
                    cves.update(package_cves[name])
        scored, failed = {}, {}
        for batch in epss_batches(cves):
            scoped = owner.for_hosts(HOSTS, max_age=age)
            try:
                scores = epss_scores(batch, scoped)
                scored.update({cve: (scores.get(cve), tuple(scoped.observed)) for cve in batch})
            except Exception as error:
                failed.update(dict.fromkeys(batch, type(error).__name__))
        for name, _, _ in items:
            entries, times = prepared[name]
            if not isinstance(entries, list):
                continue
            ids = package_cves[name]
            scores = {cve: scored[cve][0] for cve in ids if cve in scored and scored[cve][0] is not None}
            observed = {stamp for cve in ids if cve in scored for stamp in scored[cve][1]}
            errors = sorted({failed[cve] for cve in ids if cve in failed})
            prepared[name] = (entries, (*times, *sorted(observed)), (scores, errors))
    return prepared


def check_prepared(subject, settings, io, prepared):
    entries, observed, *enrichment = prepared
    if isinstance(entries, Exception):
        raise entries
    io.observed.extend(observed)
    return render(subject, settings, io, entries, *enrichment)


def group_aliases(entries):
    # Connected alias sets, rather than one result per provider spelling.
    groups = []
    for entry in entries:
        aliases = {entry['id'], *entry.get('aliases', [])}
        matching = [g for g in groups if g[0] & aliases]
        members = [entry]
        for old in matching:
            aliases |= old[0]
            members += old[1]
            groups.remove(old)
        groups.append((aliases, members))
    return groups


def matching_ranges(affected, settings):
    ranges = affected.get('ranges', [])
    if 'commit' in settings or settings.get('ecosystem') == 'GIT':
        repository = (settings['repository'] if 'commit' in settings else settings['name']).rstrip('/').removesuffix('.git')
        return [item for item in ranges if item.get('type') == 'GIT'
                and str(item.get('repo', '')).rstrip('/').removesuffix('.git') == repository]
    package = affected.get('package', {})
    return ranges if (package.get('name'), package.get('ecosystem')) == (
        settings.get('name'), settings.get('ecosystem')) else []


def public_reference(value):
    """Validate a display link without resolving or fetching its destination."""
    if not isinstance(value, str) or len(value) > 8192 or re.search(r'[\s\\<>"`]', value):
        return None
    try:
        url = urlsplit(value)
        host = (url.hostname or '').rstrip('.').lower()
        if url.scheme != 'https' or not host or url.username or url.password:
            return None
        if url.port is not None and not 1 <= url.port <= 65535:
            return None
        try:
            if not ip_address(host).is_global:
                return None
        except ValueError:
            # Reject single-label/intranet names and noncanonical numeric hosts.
            labels = host.encode('idna').decode('ascii').split('.')
            if (len(labels) < 2 or not re.fullmatch(r'[a-z][a-z0-9-]*', labels[-1])
                    or labels[-1] in {'localhost', 'local', 'internal', 'invalid', 'test'}
                    or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
                           for part in labels)):
                return None
    except (ValueError, UnicodeError):
        return None
    return value


def attributed_context(member, settings, member_url, *, provider='OSV'):
    """Normalize optional context; provider prose remains untrusted plain text."""
    facts = []
    summary = member.get('summary')
    if isinstance(summary, str) and summary.strip():
        summary = ' '.join(summary.split())
        facts.append(evidence('Summary', summary[:600] + ('…' if len(summary) > 600 else ''),
                              provider, member_url, code='summary'))
        if len(summary) > 600:
            facts.append(evidence('Summary characters omitted', len(summary) - 600,
                                  provider, member_url, code='truncated'))
    severities = list(member.get('severity') or []) if isinstance(member.get('severity'), list) else []
    for affected in member.get('affected', []):
        package = affected.get('package', {})
        matches = (bool(matching_ranges(affected, settings)) if 'commit' in settings or settings.get('ecosystem') == 'GIT' else
                   (package.get('name'), package.get('ecosystem')) == (settings.get('name'), settings.get('ecosystem')))
        if matches:
            if isinstance(affected.get('severity'), list):
                severities.extend(affected['severity'])
    prefixes = {'CVSS_V2': r'(?:CVSS:2\.0/)?', 'CVSS_V3': r'CVSS:3\.[01]/',
                'CVSS_V4': r'CVSS:4\.0/'}
    for severity in severities:
        if not isinstance(severity, dict):
            continue
        kind, vector = severity.get('type'), severity.get('score')
        if not isinstance(kind, str) or not isinstance(vector, str):
            continue
        kind, vector = kind.strip().upper(), vector.strip()
        if (kind not in prefixes or len(vector) > 512
                or not re.fullmatch(prefixes[kind] + r'AV:[A-Za-z]+(?:/[A-Za-z][A-Za-z0-9]*:[A-Za-z0-9.]+)+', vector)):
            continue
        origin = severity.get('source')
        key = kind + (' · ' + origin if origin in ('NVD', 'CNA', 'SELF') else '')
        facts.append(evidence(key, vector, provider, member_url, code='cvss_vector'))
    return facts


def reference_facts(members, advisory_url, *, provider='OSV'):
    priorities = {'FIX': 0, 'ADVISORY': 1, 'WEB': 2}
    references = {}
    for member in members:
        items = member.get('references')
        if not isinstance(items, list):
            continue
        for item in items:
            if (not isinstance(item, dict) or not isinstance(item.get('type'), str)
                    or item['type'] not in priorities):
                continue
            url = public_reference(item.get('url'))
            if url and (url not in references or priorities[item['type']] < priorities[references[url]]):
                references[url] = item['type']
    ordered = sorted(references, key=lambda url: (priorities[references[url]], url))
    facts = [evidence(references[url], url, provider, url, code='reference') for url in ordered[:12]]
    if len(ordered) > 12:
        facts.append(evidence('Additional references', len(ordered) - 12, provider, advisory_url, code='truncated'))
    return facts


def bounded_facts(facts, provider, advisory_url):
    # Alias responses can repeat the same facts. Keep their source links while
    # avoiding false evidence revisions from provider ordering or duplication.
    unique = {json.dumps(fact, sort_keys=True): fact for fact in facts}
    priority = {'query': 0, 'epss_probability': 1, 'fixed_events': 2, 'kev_added': 2,
                'kev_ransomware': 2, 'summary': 3, 'reference': 4, 'cvss_vector': 5, 'truncated': 6}
    ordered = [unique[key] for key in sorted(
        unique, key=lambda key: (priority.get(unique[key].get('code'), 1), key))]
    if len(ordered) > 256:
        omitted = len(ordered) - 255
        ordered = ordered[:255] + [evidence('Additional evidence fields', omitted, provider,
                                           advisory_url, code='truncated')]
    return ordered



def cve_ids(entries):
    return {value for row in entries for value in (row['id'], *row.get('aliases', []))
            if CVE.fullmatch(value)}


def epss_batches(cves):
    batch, length = [], 0
    for cve in sorted(set(cves)):
        if len(cve) > 2000:
            # An invalid upstream identifier cannot fit into the provider protocol.
            yield [cve]
            continue
        if len(batch) == 100 or length + len(cve) + bool(batch) > 2000:
            yield batch
            batch, length = [], 0
        length += len(cve) + bool(batch)
        batch.append(cve)
    if batch:
        yield batch


def epss_scores(batch, io):
    parameter = ','.join(batch)
    if len(parameter) > 2000:
        raise ValueError('EPSS identifier exceeds query budget')
    data = io.json('GET', 'https://api.first.org/data/v1/epss?cve=' + parameter)
    rows = data['data']
    if not isinstance(rows, list) or data.get('total', len(rows)) not in (len(rows), str(len(rows))):
        raise ValueError('Incomplete EPSS response')
    result = {}
    requested = set(batch)
    for row in rows:
        cve, day, probability = row['cve'], row['date'], float(row['epss'])
        if cve not in requested or cve in result or not 0 <= probability <= 1 or date.fromisoformat(day).isoformat() != day:
            raise ValueError('Invalid EPSS score')
        result[cve] = (probability, day)
    return result


def load_entries(subject, settings, io):
    return nvd.read(subject, settings, io) if 'vendor' in settings else osv(subject, settings, io)


def check(subject, settings, io):
    return render(subject, settings, io, load_entries(subject, settings, io))


def render(subject, settings, io, entries, enrichment=None):
    if entries is None:
        return {"status": "unsupported", "findings": [], "note": "Upstream version identity is not established."}
    provider = "NVD" if "vendor" in settings else "OSV"
    source_url = nvd.query_url(settings, subject['version']) if provider == "NVD" else "https://api.osv.dev/v1/query"
    if provider == 'NVD':
        queried = {**{k: v for k, v in settings.items() if k != 'source'}, 'version': subject['version']}
    else:
        request = query(subject, settings)
        if request is None:
            return {"status": "unsupported", "findings": [], "note": "Upstream version identity is not established."}
        queried = {'commit': request['commit']} if 'commit' in request else {**request['package'], 'version': request['version']}
    query_facts = [evidence("Query " + key, value, provider, source_url, code="query")
                   for key, value in queried.items()]
    groups = group_aliases(entries)
    cves = sorted({a for aliases, _ in groups for a in aliases if CVE.fullmatch(a)})
    kev, epss, errors = {}, {}, []
    kev_ok = False
    if cves:
        try:
            data = io.json("GET", "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json")
            kev = {v["cveID"]: v for v in data["vulnerabilities"]}
            kev_ok = True
        except Exception as error:
            errors.append("KEV: " + type(error).__name__)
        if enrichment is not None:
            epss, failed = enrichment
            errors.extend('EPSS: ' + reason for reason in failed)
        else:
            for batch in epss_batches(cves):
                try:
                    epss.update(epss_scores(batch, io))
                except Exception as error:
                    errors.append('EPSS: ' + type(error).__name__)
    findings = []
    for aliases, members in sorted(groups, key=lambda group: min(group[0])):
        members = sorted(members, key=lambda item: (item["id"], json.dumps(item, sort_keys=True)))
        ids = sorted(a for a in aliases if CVE.fullmatch(a))
        identity = ids[0] if ids else min(aliases)
        active = bool(set(ids) & set(kev))
        link = (
            "https://nvd.nist.gov/vuln/detail/" + identity
            if ids
            else "https://osv.dev/vulnerability/" + quote(identity, safe="")
        )
        facts = list(query_facts)
        if 'commit' in settings:
            facts.append(evidence('Source repository', settings['repository'], 'RPM Source0',
                                  settings['repository'], code='query'))
        aliases_url = (
            link if provider == "NVD" else "https://osv.dev/vulnerability/" + quote(members[0]["id"], safe="")
        )
        facts.append(evidence("Aliases", sorted(aliases - {identity}), provider, aliases_url))
        for member in members:
            member_url = (
                "https://nvd.nist.gov/vuln/detail/" + member["id"]
                if provider == 'NVD'
                else "https://osv.dev/vulnerability/" + quote(member["id"], safe="")
            )
            facts.append(evidence("Returned advisory", member["id"], provider, member_url, code="advisory_id"))
            fixed = set()
            for affected in member.get("affected", []):
                for affected_range in matching_ranges(affected, settings):
                    for event in affected_range.get("events", []):
                        if event.get("fixed"):
                            fixed.add(str(event["fixed"]))
            if provider == "OSV":
                facts.append(evidence("Fixed events", sorted(fixed), provider, member_url, code="fixed_events"))
            if provider == 'NVD':
                facts.append(evidence('Vulnerable CPE criteria', member['cpe_matches'], 'NVD', member_url, code='cpe_match'))
            facts.extend(attributed_context(member, settings, member_url, provider=provider))
        facts.extend(reference_facts(members, aliases_url, provider=provider))
        kev_url = "https://www.cisa.gov/known-exploited-vulnerabilities-catalog"
        if not ids:
            facts.append(evidence("KEV (no CVE alias)", None, "CISA", kev_url, status="not_applicable"))
            facts.append(
                evidence("EPSS (no CVE alias)", None, "FIRST", "https://www.first.org/epss/", status="not_applicable")
            )
        for cve in ids:
            facts.append(
                evidence(
                    "KEV · " + cve,
                    cve in kev if kev_ok else None,
                    "CISA",
                    kev_url,
                    status="observed" if kev_ok else "unavailable",
                )
            )
            if cve in kev:
                record = kev[cve]
                day = record.get('dateAdded')
                try:
                    if isinstance(day, str) and date.fromisoformat(day).isoformat() == day:
                        facts.append(evidence('KEV added · ' + cve, day, 'CISA', kev_url, code='kev_added'))
                except ValueError:
                    # An invalid optional date does not discard the KEV membership evidence.
                    pass
                ransomware = record.get('knownRansomwareCampaignUse')
                if ransomware in ('Known', 'Unknown'):
                    facts.append(evidence('Ransomware campaign use · ' + cve, ransomware,
                                          'CISA', kev_url, code='kev_ransomware'))
            epss_url = "https://api.first.org/data/v1/epss?cve=" + cve
            if cve in epss:
                probability, day = epss[cve]
                facts.append(evidence("EPSS probability · " + cve, probability, "FIRST", epss_url, code="epss_probability"))
                facts.append(evidence("EPSS model date · " + cve, day, "FIRST", epss_url, code="epss_date"))
            else:
                facts.append(evidence("EPSS · " + cve, None, "FIRST", epss_url, status="unavailable"))
        findings.append(finding(identity, Issue.ADVISORY, identity, bounded_facts(facts, provider, aliases_url),
                                link, tags=["KEV"] if active else []))
    return {
        "status": "partial" if errors else "ok",
        "findings": findings,
        "note": ("Repository query; subpackage and local-patch applicability are not evaluated."
                 if 'commit' in settings or settings.get('ecosystem') == 'GIT' else
                 "Current source component only; local patches, build options, bundled dependencies and binary artifacts are not evaluated.")
        + (" Enrichment incomplete: " + "; ".join(errors) if errors else ""),
    }
