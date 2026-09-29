"""Security candidates, not a claim that distribution backports are absent."""

from datetime import date
from ipaddress import ip_address
import json
import re
from urllib.parse import quote, urlsplit

from tracker.monitors.model import evidence, finding, version_query as query_subject
from tracker.monitors.issues import Issue
from tracker.monitors.schedule import Schedule
from tracker.monitors.source.release import commit_hash


TITLE = Issue.ADVISORY
VERSION = 7
HOSTS = {"api.osv.dev", "www.cisa.gov", "api.first.org"}
CVE = re.compile(r"CVE-\d{4}-\d{4,}")


def refresh(subject, inputs, previous):
    # New advisories/KEV entries can arrive without any source-version change.
    return Schedule(interval_seconds=21600)


def inputs(package, configured):
    if configured is not None and 'vendor' in configured:
        return configured
    from tracker.identity import from_package
    identity = configured if configured is not None else from_package(package)
    if identity and 'commit' in identity:
        return identity
    if identity and identity.get('ecosystem') in ('PyPI', 'crates.io', 'Go', 'npm'):
        if query_version(package.get('version'), identity['ecosystem']):
            return identity
        return package.get('source_commit') or identity
    return configured if configured is not None else package.get('source_commit')


def query_version(version, ecosystem):
    if ecosystem == 'PyPI':
        from packaging.version import Version, InvalidVersion
        try:
            Version(version)
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
    if set(settings) != {'ecosystem', 'name'} or settings['ecosystem'] not in ('PyPI', 'crates.io', 'Go', 'npm'):
        raise ValueError('security requires an explicit supported ecosystem/name')
    if not query_version(subject['version'], settings['ecosystem']):
        return None
    return {'package': settings, 'version': subject['version']}


def osv(subject, settings, io):
    request = query(subject, settings)
    if request is None:
        return None
    entries, token, seen = [], None, set()
    for _ in range(20):
        response = io.json('POST', 'https://api.osv.dev/v1/query', {**request, **({'page_token': token} if token else {})})
        for item in response.get('vulns', []):
            if 'affected' not in item:
                item = io.json('GET', 'https://api.osv.dev/v1/vulns/' + quote(item['id'], safe=''))
            if not item.get('withdrawn'):
                entries.append(item)
        token = response.get('next_page_token')
        if not token:
            return entries
        if token in seen:
            break
        seen.add(token)
    raise ValueError('OSV pagination exceeds budget')


def group_aliases(entries):
    # Connected alias sets, rather than one result per provider spelling.
    groups = []
    for entry in entries:
        aliases = {entry['id'], *entry.get('aliases', [])}
        matching = [g for g in groups if g[0] & aliases]
        members = [entry]
        for old in matching:
            aliases |= old[0]; members += old[1]; groups.remove(old)
        groups.append((aliases, members))
    return groups


def matching_ranges(affected, settings):
    ranges = affected.get('ranges', [])
    if 'commit' in settings:
        repository = settings['repository'].removesuffix('.git').rstrip('/')
        return [item for item in ranges if item.get('type') == 'GIT'
                and str(item.get('repo', '')).removesuffix('.git').rstrip('/') == repository]
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


def attributed_context(member, settings, member_url):
    """Normalize optional OSV context; provider prose remains untrusted plain text."""
    facts = []
    summary = member.get('summary')
    if isinstance(summary, str) and summary.strip():
        summary = ' '.join(summary.split())
        facts.append(evidence('Summary', summary[:600] + ('…' if len(summary) > 600 else ''),
                              'OSV', member_url, code='summary'))
        if len(summary) > 600:
            facts.append(evidence('Summary characters omitted', len(summary) - 600,
                                  'OSV', member_url, code='truncated'))
    severities = list(member.get('severity') or []) if isinstance(member.get('severity'), list) else []
    for affected in member.get('affected', []):
        package = affected.get('package', {})
        matches = (bool(matching_ranges(affected, settings)) if 'commit' in settings else
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
        facts.append(evidence(key, vector, 'OSV', member_url, code='cvss_vector'))
    return facts


def reference_facts(members, advisory_url):
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
    facts = [evidence(references[url], url, 'OSV', url, code='reference') for url in ordered[:12]]
    if len(ordered) > 12:
        facts.append(evidence('Additional references', len(ordered) - 12, 'OSV', advisory_url, code='truncated'))
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


def check(subject, settings, io):
    if "vendor" in settings:
        # The scanner adapter is isolated from page rendering and OSV transport.
        from tracker.monitors.security.cve import scan

        entries = scan(subject, settings)
    else:
        entries = osv(subject, settings, io)
    if entries is None:
        return {"status": "unsupported", "findings": [], "note": "Upstream version identity is not established."}
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
        try:
            for start in range(0, len(cves), 100):
                data = io.json("GET", "https://api.first.org/data/v1/epss?cve=" + ",".join(cves[start : start + 100]))
                for row in data["data"]:
                    probability = float(row["epss"])
                    if not 0 <= probability <= 1:
                        raise ValueError("invalid EPSS probability")
                    epss[row["cve"]] = (probability, row["date"])
        except Exception as error:
            errors.append("EPSS: " + type(error).__name__)
    findings = []
    for aliases, members in sorted(groups, key=lambda group: min(group[0])):
        members = sorted(members, key=lambda item: (item["id"], json.dumps(item, sort_keys=True)))
        ids = sorted(a for a in aliases if CVE.fullmatch(a))
        identity = ids[0] if ids else min(aliases)
        active = bool(set(ids) & set(kev))
        provider = "cve-bin-tool" if "vendor" in settings else "OSV"
        link = (
            "https://nvd.nist.gov/vuln/detail/" + identity
            if ids
            else "https://osv.dev/vulnerability/" + quote(identity, safe="")
        )
        source_url = (
            "https://cve-bin-tool.readthedocs.io/en/latest/" if "vendor" in settings else "https://api.osv.dev/v1/query"
        )
        queried = {'commit': settings['commit']} if 'commit' in settings else {**settings, 'version': subject['version']}
        facts = [
            evidence("Query " + key, value, provider, source_url, code="query")
            for key, value in queried.items()
        ]
        if 'commit' in settings:
            facts.append(evidence('Source repository', settings['repository'], 'RPM Source0',
                                  settings['repository'], code='query'))
        aliases_url = (
            link if provider == "cve-bin-tool" else "https://osv.dev/vulnerability/" + quote(members[0]["id"], safe="")
        )
        facts.append(evidence("Aliases", sorted(aliases - {identity}), provider, aliases_url))
        for member in members:
            member_url = (
                "https://nvd.nist.gov/vuln/detail/" + member["id"]
                if "vendor" in settings
                else "https://osv.dev/vulnerability/" + quote(member["id"], safe="")
            )
            facts.append(evidence("Returned advisory", member["id"], provider, member_url))
            fixed = set()
            for affected in member.get("affected", []):
                for affected_range in matching_ranges(affected, settings):
                    for event in affected_range.get("events", []):
                        if event.get("fixed"):
                            fixed.add(str(event["fixed"]))
            if provider == "OSV":
                facts.append(evidence("Fixed events", sorted(fixed), provider, member_url, code="fixed_events"))
                facts.extend(attributed_context(member, settings, member_url))
        if provider == "OSV":
            facts.extend(reference_facts(members, aliases_url))
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
                    pass
                ransomware = record.get('knownRansomwareCampaignUse')
                if ransomware in ('Known', 'Unknown'):
                    facts.append(evidence('Ransomware campaign use · ' + cve, ransomware,
                                          'CISA', kev_url, code='kev_ransomware'))
            epss_url = "https://api.first.org/data/v1/epss?cve=" + cve
            if cve in epss:
                probability, day = epss[cve]
                facts.append(evidence("EPSS probability · " + cve, probability, "FIRST", epss_url, code="epss_probability"))
                facts.append(evidence("EPSS model date · " + cve, day, "FIRST", epss_url))
            else:
                facts.append(evidence("EPSS · " + cve, None, "FIRST", epss_url, status="unavailable"))
        findings.append(finding(identity, Issue.ADVISORY, identity, bounded_facts(facts, provider, aliases_url),
                                link, tags=["KEV"] if active else []))
    return {
        "status": "partial" if errors else "ok",
        "findings": findings,
        "note": ("Repository commit query; subpackage applicability is not evaluated."
                 if 'commit' in settings else "Current source component only; bundled dependencies and binary artifacts are not covered.")
        + (" Enrichment incomplete: " + "; ".join(errors) if errors else ""),
    }
