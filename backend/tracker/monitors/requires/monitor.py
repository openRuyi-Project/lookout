"""Exact upstream release declarations; comparison and local assessment are read-side."""
from typing import Protocol

from tracker.monitors.model import finding, fingerprint
from tracker.monitors.requires import cpan as requires_cpan, cratesio as requires_cratesio, pypi as requires_pypi
from tracker.monitors.requires.model import Requirement, UnsupportedRequirements, key
from tracker.monitors.schedule import Schedule
from tracker.providers.model import UnsupportedRelease


class Backend(Protocol):
    HOSTS: set[str]

    def inputs(self, package: dict, configured: dict | None) -> dict | None: ...

    def read(self, version: str, settings: dict, io) -> list[Requirement]: ...


TITLE = 'Dependencies'
VERSION = 5
SCOPE = 'current_and_upgrade'
BACKENDS: dict[str, Backend] = {'pypi': requires_pypi, 'cratesio': requires_cratesio, 'cpan': requires_cpan}
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
    return Schedule(interval_seconds=43200)


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
                    scope + ':' + fingerprint(identity), TITLE,
                    declaration.name + ' ' + (declaration.declaration.strip() or 'any version'),
                    [], declaration.url, scope=scope,
                    target_version=version if scope == 'upgrade' else None, requirement=fact))
            checks[scope] = {'status': 'ok', 'note': None}
            provides = getattr(backend, 'provides', None)
            if provides:
                checks[scope]['provides'] = provides(version, settings, io)
            findings.extend(release_findings)
        except (UnsupportedRequirements, UnsupportedRelease) as error:
            checks[scope] = {'status': 'unsupported', 'note': str(error)}
        except Exception:
            checks[scope] = {'status': 'error', 'note': 'Upstream release requirements could not be read.'}
    successful = sum(c['status'] == 'ok' for c in checks.values())
    status = ('ok' if successful == len(checks) else 'partial' if successful
              else 'error' if any(c['status'] == 'error' for c in checks.values()) else 'unsupported')
    notes = [scope + ': ' + item['note'] for scope, item in checks.items() if item['note']]
    return {'status': status, 'findings': findings, 'note': '; '.join(notes) or None, 'scope_checks': checks}
