"""NVD owns CPE/version matching; this adapter does not invent a range comparator."""
import re
from urllib.parse import urlencode


URL = 'https://services.nvd.nist.gov/rest/json/cves/2.0'
MIN_INTERVAL = 6.5


def cpe(settings, version):
    if (set(settings) - {'vendor', 'product', 'part', 'source'}
            or not {'vendor', 'product'} <= set(settings)
            or settings.get('source', 'nvd') != 'nvd'
            or settings.get('part', 'a') not in ('a', 'o', 'h')
            or any(not isinstance(settings[k], str) or not re.fullmatch(r'[a-z0-9._-]{1,120}', settings[k])
                   for k in ('vendor', 'product'))):
        raise ValueError('NVD requires a reviewed CPE part/vendor/product')
    if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,199}', version):
        return None
    return f"cpe:2.3:{settings.get('part', 'a')}:{settings['vendor']}:{settings['product']}:{version}:*:*:*:*:*:*:*"


def query_url(settings, version, start=0):
    name = cpe(settings, version)
    if name is None:
        return None
    return URL + '?' + urlencode({'cpeName': name, 'isVulnerable': '', 'noRejected': '',
                                  'resultsPerPage': 200, 'startIndex': start})


def matches(record, settings):
    expected = ['cpe', '2.3', settings.get('part', 'a'), settings['vendor'], settings['product']]
    result = []
    pending = list(record.get('configurations') or [])
    while pending:
        node = pending.pop()
        if not isinstance(node, dict) or node.get('negate'):
            continue
        pending.extend(node.get('nodes') or [])
        pending.extend(node.get('children') or [])
        for match in node.get('cpeMatch') or []:
            if not isinstance(match, dict) or match.get('vulnerable') is not True:
                continue
            criteria = match.get('criteria')
            if not isinstance(criteria, str) or criteria.split(':')[:5] != expected:
                continue
            bounds = [('>=', 'versionStartIncluding'), ('>', 'versionStartExcluding'),
                      ('<=', 'versionEndIncluding'), ('<', 'versionEndExcluding')]
            conditions = [op + ' ' + str(match[key]) for op, key in bounds if key in match]
            result.append(criteria + ('; ' + ', '.join(conditions) if conditions else ''))
    return sorted(set(result))


def normalize(record, settings):
    identity = record.get('id')
    if not isinstance(identity, str) or not re.fullmatch(r'CVE-\d{4}-\d{4,}', identity):
        raise ValueError('NVD returned an invalid advisory identity')
    if record.get('vulnStatus') == 'Rejected':
        return None
    criteria = matches(record, settings)
    if not criteria:
        raise ValueError('NVD returned an advisory without the queried vulnerable CPE')
    summary = next((d.get('value') for d in record.get('descriptions', [])
                    if isinstance(d, dict) and d.get('lang') == 'en'), None)
    severity = []
    for key, rows in (record.get('metrics') or {}).items():
        kind = {'cvssMetricV2': 'CVSS_V2', 'cvssMetricV31': 'CVSS_V3',
                'cvssMetricV30': 'CVSS_V3', 'cvssMetricV40': 'CVSS_V4'}.get(key)
        if kind and isinstance(rows, list):
            severity.extend({'type': kind, 'score': row.get('cvssData', {}).get('vectorString')}
                            for row in rows if isinstance(row, dict))
    references = []
    for item in record.get('references') or []:
        if isinstance(item, dict):
            tags = item.get('tags') or []
            kind = 'FIX' if 'Patch' in tags else 'ADVISORY' if 'Vendor Advisory' in tags else 'WEB'
            references.append({'type': kind, 'url': item.get('url')})
    return {'id': identity, 'aliases': [], 'affected': [], 'summary': summary,
            'severity': severity, 'references': references, 'cpe_matches': criteria}


def read(subject, settings, io):
    start, expected_total, entries = 0, None, []
    if cpe(settings, subject['version']) is None:
        return None
    for _ in range(50):
        response = io.json('GET', query_url(settings, subject['version'], start), min_interval=MIN_INTERVAL)
        total, rows = response.get('totalResults'), response.get('vulnerabilities')
        if (type(total) is not int or not 0 <= total <= 10000
                or response.get('startIndex') != start or not isinstance(rows, list)
                or (expected_total is not None and total != expected_total)):
            raise ValueError('NVD pagination/result changed during this check')
        expected_total = total
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('cve'), dict):
                raise ValueError('NVD returned an invalid advisory')
            entry = normalize(row['cve'], settings)
            if entry:
                entries.append(entry)
        start += len(rows)
        if start == total:
            return entries
        if not rows or start > total:
            break
    raise ValueError('NVD pagination exceeds budget')
