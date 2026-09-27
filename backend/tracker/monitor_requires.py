"""Exact upstream release declarations; comparison and local assessment are read-side."""
from typing import Protocol

from . import requires_cratesio, requires_pypi
from .monitor_model import finding, fingerprint, version_query as query_subject
from .requirements import Requirement, UnsupportedRequirements, key
from .schedule import Schedule


class Backend(Protocol):
    HOSTS: set[str]

    def inputs(self, package: dict, configured: dict | None) -> dict | None: ...

    def read(self, version: str, settings: dict, io) -> list[Requirement]: ...


TITLE = 'Requires'
VERSION = 2
SCOPE = 'current_and_upgrade'
BACKENDS: dict[str, Backend] = {'pypi': requires_pypi, 'cratesio': requires_cratesio}
HOSTS = set().union(*(backend.HOSTS for backend in BACKENDS.values()))


def inputs(package, configured):
    if configured is not None:
        if not isinstance(configured, dict) or len(configured) != 1:
            raise ValueError('Requires needs one supported provider identity')
        provider = next(iter(configured))
        if provider not in BACKENDS:
            raise ValueError('unsupported Requires provider')
        return BACKENDS[provider].inputs(package, configured)
    for backend in BACKENDS.values():
        resolved = backend.inputs(package, None)
        if resolved is not None:
            return resolved
    return None


def refresh(subject, inputs, previous):
    # Optionality adds compatible evidence. Backfill it through the regular
    # bounded queue without invalidating still-useful version-2 observations.
    needs_optional = previous.get('status') != 'unsupported' and any(isinstance(item.get('requirement'), dict)
                         and 'optional' not in item['requirement']
                         for item in previous.get('findings', []))
    return Schedule(interval_seconds=30 if needs_optional else 43200)


def check(subject, settings, io):
    if not isinstance(settings, dict) or len(settings) != 1:
        raise ValueError('Requires needs one supported provider identity')
    backend = BACKENDS.get(next(iter(settings)))
    if backend is None:
        raise ValueError('unsupported Requires provider')
    findings, checks = [], {}
    for scope, version in [('current', subject['version']), ('upgrade', subject.get('target_version'))]:
        if not version:
            continue
        try:
            declarations = backend.read(version, settings, io)
            seen = set()
            release_findings = []
            for declaration in declarations:
                fact = declaration.fact()
                identity = key(fact)
                if identity in seen:
                    raise ValueError('duplicate dependency declaration')
                seen.add(identity)
                release_findings.append(finding(
                    scope + ':' + fingerprint(identity), 'Requires',
                    declaration.name + ' ' + (declaration.declaration.strip() or 'any version'),
                    [], declaration.url, scope=scope,
                    target_version=version if scope == 'upgrade' else None, requirement=fact))
            findings.extend(release_findings)
            checks[scope] = {'status': 'ok', 'note': None}
        except UnsupportedRequirements as error:
            checks[scope] = {'status': 'unsupported', 'note': str(error)}
        except Exception:
            checks[scope] = {'status': 'error', 'note': 'Upstream release requirements could not be read.'}
    successful = sum(c['status'] == 'ok' for c in checks.values())
    status = ('ok' if successful == len(checks) else 'partial' if successful
              else 'error' if any(c['status'] == 'error' for c in checks.values()) else 'unsupported')
    notes = [scope + ': ' + item['note'] for scope, item in checks.items() if item['note']]
    return {'status': status, 'findings': findings, 'note': '; '.join(notes) or None, 'scope_checks': checks}
