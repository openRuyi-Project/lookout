"""Lifecycle adapter. Explicit lifecycle policy takes precedence over official identity mappings."""

import re
from datetime import date
from urllib.parse import quote, unquote

from packaging.utils import canonicalize_name

from tracker.identity import from_package
from tracker.monitors.model import evidence, finding
from tracker.monitors.schedule import Schedule

TITLE = 'EOL'
VERSION = 2
HOSTS = {"endoflife.date"}
MAPPING_URL = 'https://endoflife.date/api/v1/identifiers/purl'
PURL_TYPES = {'PyPI': 'pypi', 'crates.io': 'cargo', 'Go': 'golang', 'npm': 'npm'}


def refresh(subject, inputs, previous):
    return Schedule(interval_seconds=21600)


def inputs(package, configured):
    if configured is not None:
        return configured
    identity = from_package(package)
    if not identity or not (kind := PURL_TYPES.get(identity['ecosystem'])):
        return None
    name = identity['name']
    if kind == 'pypi':
        name = canonicalize_name(name)
    return {'purl': 'pkg:' + kind + '/' + quote(name, safe='/')}


def release_cycle(version, parts):
    if type(parts) is not int or not 1 <= parts <= 3:
        raise ValueError('cycle_parts must be in 1..3')
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:[a-z][0-9]*)?", version or '')
    if not match or len(match[1].split('.')) < parts:
        return None
    return '.'.join(match[1].split('.')[:parts])


def query_subject(subject, settings):
    if 'purl' in settings:
        return {'version': subject['version']}
    return {'cycle': release_cycle(subject['version'], settings['cycle_parts'])}



def mapped_products(purl, io):
    data = io.json('GET', MAPPING_URL)
    rows = data['result']
    if not isinstance(rows, list) or data.get('total', len(rows)) != len(rows):
        raise ValueError('Incomplete lifecycle identity mapping')
    return {row['product']['name'] for row in rows
            if unquote(row['identifier']) == unquote(purl)}


def mapped_cycle(version, releases):
    matches = []
    for row in releases:
        name = row['name']
        latest = (row.get('latest') or {}).get('name')
        if version == name or version == latest:
            matches.append(row)
        elif (re.fullmatch(r'\d+(?:\.\d+){0,2}', name)
              and release_cycle(version, name.count('.') + 1) == name
              and release_cycle(latest, name.count('.') + 1) == name):
            matches.append(row)
    return matches[0] if len(matches) == 1 else None


def unsupported(note):
    return {'status': 'unsupported', 'findings': [], 'note': note}


def check(subject, settings, io):
    purl = settings.get('purl')
    if set(settings) == {'purl'}:
        products = mapped_products(purl, io)
        if len(products) != 1:
            return unsupported('No unique provider lifecycle product for the upstream identity.')
        product, = products
        cycle = None
    else:
        if set(settings) != {'product', 'cycle_parts'}:
            raise ValueError('unsupported lifecycle configuration')
        product = settings['product']
        cycle = release_cycle(subject['version'], settings['cycle_parts'])
        if cycle is None:
            return unsupported('Version does not establish a release cycle.')
    if not isinstance(product, str) or not re.fullmatch(r"[a-z0-9-]+", product):
        raise ValueError('invalid lifecycle product slug')
    data = io.json('GET', 'https://endoflife.date/api/v1/products/' + quote(product, safe='') + '/')
    releases = data['result']['releases']
    if purl is not None:
        release = mapped_cycle(subject['version'], releases)
        if release is None:
            return unsupported(f'Version does not map to one release cycle for {product}.')
        cycle = release['name']
    else:
        release = next((r for r in releases if r['name'] == cycle), None)
        if release is None:
            return unsupported(f'Provider has no cycle {cycle} for {product}.')
    assert cycle is not None
    eol = release.get("eolFrom")
    if eol is not None:
        ended = date.fromisoformat(eol) <= io.today
    elif type(release.get("isEol")) is bool:
        ended = release["isEol"]
    else:
        return {"status": "unsupported", "findings": [], "note": "Provider has not established an EOL date or status."}
    findings = []
    if ended:
        url = "https://endoflife.date/" + product
        facts = [
            evidence("Product", product, "endoflife.date", url),
            evidence("Cycle", cycle, "endoflife.date", url),
            evidence("EOL", ended, "endoflife.date", url),
        ]
        if purl is not None:
            facts.append(evidence('Package URL', purl, 'endoflife.date', MAPPING_URL))
        if eol:
            facts.append(evidence("EOL date", eol, "endoflife.date", url))
        findings.append(finding("cycle:" + cycle, "EOL", product + " " + cycle, facts, url))
    return {"status": "ok", "findings": findings, "note": f"Current source cycle {cycle}; upstream lifecycle only."}
