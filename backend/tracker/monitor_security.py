"""Security candidates, not a claim that distribution backports are absent."""

import re
from urllib.parse import quote
from .monitor_model import finding, evidence

VERSION = 4
HOSTS = {"api.osv.dev", "www.cisa.gov", "api.first.org"}
CVE = re.compile(r"CVE-\d{4}-\d{4,}")


def inputs(package, configured):
    if configured is not None:
        return configured
    from .package_identity import from_native
    return from_native(package['identity'])


def osv(subject, settings, io):
    if set(settings) != {'ecosystem', 'name'} or settings['ecosystem'] not in ('PyPI', 'crates.io', 'Go', 'npm'):
        raise ValueError('security requires an explicit supported ecosystem/name')
    version = subject['version']
    if settings['ecosystem'] == 'PyPI':
        from packaging.version import Version, InvalidVersion
        try:
            Version(version)
        except InvalidVersion:
            return None
    if settings['ecosystem'] in ('Go', 'crates.io') and not re.fullmatch(r'v?[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?', version):
        return None
    query = {'package': {'ecosystem': settings['ecosystem'], 'name': settings['name']}, 'version': version}
    entries, token, seen = [], None, set()
    for _ in range(20):
        response = io.json('POST', 'https://api.osv.dev/v1/query', {**query, **({'page_token': token} if token else {})})
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


def check(subject, settings, io):
    if "vendor" in settings:
        # The scanner adapter is isolated from page rendering and OSV transport.
        from .monitor_cve import scan

        entries = scan(subject, settings)
    else:
        entries = osv(subject, settings, io)
    if entries is None:
        return {"status": "unsupported", "findings": [], "note": "Upstream version identity is not established."}
    groups = group_aliases(entries)
    cves = sorted({a for aliases, _ in groups for a in aliases if CVE.fullmatch(a)})
    kev, epss, errors = set(), {}, []
    kev_ok = False
    if cves:
        try:
            data = io.json("GET", "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json")
            kev = {v["cveID"] for v in data["vulnerabilities"]}
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
    for aliases, members in groups:
        ids = sorted(a for a in aliases if CVE.fullmatch(a))
        identity = ids[0] if ids else min(aliases)
        active = bool(set(ids) & kev)
        provider = "cve-bin-tool" if "vendor" in settings else "OSV"
        link = (
            "https://nvd.nist.gov/vuln/detail/" + identity
            if ids
            else "https://osv.dev/vulnerability/" + quote(identity, safe="")
        )
        source_url = (
            "https://cve-bin-tool.readthedocs.io/en/latest/" if "vendor" in settings else "https://api.osv.dev/v1/query"
        )
        facts = [
            evidence("Query " + key, value, provider, source_url)
            for key, value in {**settings, "version": subject["version"]}.items()
        ]
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
                package = affected.get("package", {})
                if (package.get("name"), package.get("ecosystem")) != (settings.get("name"), settings.get("ecosystem")):
                    continue
                for affected_range in affected.get("ranges", []):
                    for event in affected_range.get("events", []):
                        if event.get("fixed"):
                            fixed.add(str(event["fixed"]))
            if provider == "OSV":
                facts.append(evidence("Fixed events", sorted(fixed), provider, member_url))
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
            epss_url = "https://api.first.org/data/v1/epss?cve=" + cve
            if cve in epss:
                probability, day = epss[cve]
                facts.append(evidence("EPSS probability · " + cve, probability, "FIRST", epss_url))
                facts.append(evidence("EPSS model date · " + cve, day, "FIRST", epss_url))
            else:
                facts.append(evidence("EPSS · " + cve, None, "FIRST", epss_url, status="unavailable"))
        findings.append(finding(identity, "Security", identity, facts, link, tags=["KEV"] if active else []))
    return {
        "status": "partial" if errors else "ok",
        "findings": findings,
        "note": "Current source component only; bundled dependencies and binary artifacts are not covered."
        + (" Enrichment incomplete: " + "; ".join(errors) if errors else ""),
    }
