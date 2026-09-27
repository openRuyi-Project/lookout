"""Upstream dependency declarations and a separate distribution-source assessment."""
from dataclasses import dataclass
from typing import Annotated, Literal

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, Field

from tracker import config as cfg, identity as package_identity, state
from tracker.monitors.requires import compare as requirement_versions


class Constraint(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expression: Annotated[str, Field(max_length=1024)]
    source: Annotated[str, Field(min_length=1, max_length=256)]
    url: Annotated[str, Field(min_length=1, max_length=8192)]


class DependencyIdentity(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    ecosystem: Annotated[str, Field(min_length=1, max_length=100)]
    name: Annotated[str, Field(min_length=1, max_length=512)]


class Dependency(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    dependency: Annotated[str, Field(pattern=r'^[a-z][a-z0-9_.-]{0,99}$')]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    kind: Literal['runtime', 'build']
    scheme: Annotated[str, Field(pattern=r'^[a-z][a-z0-9_-]{0,39}$')]
    identity: DependencyIdentity | None = None
    condition: Annotated[str, Field(max_length=1024)] | None = None
    extras: Annotated[list[str], Field(max_length=64)] = []
    optional: bool | None = Field(default=None, description='Requires optional feature selection; null when unclassified.')


class RequirementDeclaration(Dependency):
    """A declaration for one exact upstream release, before any comparison."""
    constraint: Constraint


class DependencyObservation(BaseModel):
    version: str | None
    revision: str | None
    origin: str
    checked_at: str | None
    stale: bool


class RequirementAssessment(Dependency):
    current: Constraint | None
    target: Constraint | None
    package: str | None
    mapping: Literal['mapped', 'not_mapped', 'ambiguous', 'not_packaged']
    observed: DependencyObservation | None
    satisfaction: Literal['satisfied', 'unsatisfied', 'unknown']
    reason: str | None
    target_satisfaction: Literal['satisfied', 'unsatisfied', 'unknown'] = 'unknown'
    target_reason: str | None = None
    changed: bool = False


class UnsupportedRequirements(ValueError):
    """No reliable declaration can be recovered from this provider response."""


@dataclass(frozen=True)
class Requirement:
    dependency: str
    name: str
    kind: str
    scheme: str
    declaration: str
    comparison: object
    source: str
    url: str
    identity: dict | None = None
    condition: str | None = None
    extras: tuple[str, ...] = ()
    optional: bool | None = None

    def fact(self):
        return dict(dependency=self.dependency, name=self.name, kind=self.kind, scheme=self.scheme,
                    identity=self.identity, condition=self.condition, extras=list(self.extras), optional=self.optional,
                    constraint=dict(expression=self.declaration, source=self.source, url=self.url))


def key(item):
    identity = item.get('identity') or {}
    return (item['dependency'], item['kind'], item['scheme'], item.get('condition') or '',
            tuple(item.get('extras', [])), identity.get('ecosystem', ''), identity.get('name', ''))


def equivalent(scheme, before, after):
    try:
        if scheme == 'pep440':
            return SpecifierSet(before) == SpecifierSet(after)
        if scheme == 'numeric_minimum':
            return requirement_versions.numeric_release(before) == requirement_versions.numeric_release(after)
    except ValueError:
        return False
    return before.strip() == after.strip()



class Resolver:
    """Build one identity index per snapshot; never guess from RPM name prefixes."""
    def __init__(self, snapshot, now):
        self.snapshot, self.now = snapshot, now
        self.identities, self.observations = {}, {}
        self.packages = set(snapshot.get('sources', {})) | set(snapshot.get('inventory', {}).values())
        inventory = snapshot.get('components', {}).get('inventory', {})
        ttl = snapshot.get('obs_stale_after_seconds', snapshot.get('stale_after_seconds', 86400))
        self.inventory_current = not inventory.get('error') and not state.stale(inventory, now, ttl)
        ids = frozenset(snapshot.get('native_ids', ()))
        for package in snapshot.get('sources', {}):
            binding = cfg.resolve_binding(package, ids, snapshot.get('bindings', {}).get(package, {}))
            native = snapshot.get('tracks', {}).get(binding['compare'], {}).get('source') or {}
            identity = package_identity.from_native(native)
            if identity:
                self.identities.setdefault(self.identity_key(identity), set()).add(package)

    @staticmethod
    def identity_key(identity):
        name = identity['name']
        if identity['ecosystem'] == 'PyPI':
            name = canonicalize_name(name)
        return identity['ecosystem'], name

    def resolve(self, declaration):
        configured = self.snapshot.get('dependency_packages', {}).get(declaration['dependency'])
        if configured:
            if configured in self.packages:
                return configured, 'mapped'
            # Only the fresh, complete OBS inventory proves package absence;
            # a missing source observation or failed enumeration does not.
            return configured, 'not_packaged' if self.inventory_current else 'not_mapped'
        identity = declaration.get('identity')
        candidates = self.identities.get(self.identity_key(identity), set()) if identity else set()
        if len(candidates) == 1:
            return next(iter(candidates)), 'mapped'
        return None, 'ambiguous' if candidates else 'not_mapped'

    def observed(self, package):
        if package not in self.observations:
            source = state.current_source(self.snapshot, package)
            expired = state.stale(source, self.now, source['stale_after_seconds'])
            self.observations[package] = dict(version=source.get('version'), revision=source.get('revision'),
                origin=source['origin'], checked_at=source.get('fetched_at'),
                stale=expired or bool(source.get('error')))
        return self.observations[package]


def assess(change, snapshot, now, *, stale=False, target_stale=False, resolver=None):
    """Marks concern the declared constraint and source VERSION, not installability."""
    resolver = resolver or Resolver(snapshot, now)
    package, mapping = resolver.resolve(change)
    observed = resolver.observed(package) if mapping == 'mapped' else None

    def evaluate(constraint, unavailable):
        if constraint is None:
            return 'unknown', 'requirement_not_observed'
        if unavailable:
            return 'unknown', 'requirement_unavailable'
        if change.get('condition') or change.get('extras'):
            return 'unknown', 'condition_not_evaluated'
        if mapping != 'mapped':
            reason = {'not_mapped': 'dependency_not_mapped', 'ambiguous': 'dependency_ambiguous',
                      'not_packaged': 'dependency_unavailable'}[mapping]
            return 'unknown', reason
        if observed['stale'] or not observed['version'] or not observed['revision']:
            return 'unknown', 'dependency_unavailable'
        satisfied, reason = requirement_versions.satisfies(change['scheme'], constraint['expression'], observed['version'])
        return ('unknown', reason) if satisfied is None else ('satisfied' if satisfied else 'unsatisfied', None)

    current, target = change.get('current'), change.get('target')
    satisfaction, reason = evaluate(current, stale)
    target_satisfaction, target_reason = evaluate(target, target_stale or stale and current is None)
    changed = bool(current and target and not (stale or target_stale)
                   and not equivalent(change['scheme'], current['expression'], target['expression']))
    return {**change, 'package': package, 'mapping': mapping, 'observed': observed,
            'satisfaction': satisfaction, 'reason': reason,
            'target_satisfaction': target_satisfaction, 'target_reason': target_reason, 'changed': changed}


def project(findings, snapshot, now, resolver=None):
    """Join independently observed releases; a missing side is not addition/removal."""
    pairs = {}
    for finding in findings:
        declaration = finding.get('requirement')
        if not declaration or declaration['kind'] != 'runtime':
            continue
        identity = key(declaration)
        entry = pairs.setdefault(identity, {**{k: v for k, v in declaration.items()
                                              if k != 'constraint'},
                                            'current': None, 'target': None})
        side = 'current' if finding['scope'] == 'current' else 'target'
        entry[side] = declaration['constraint']
        entry[side + '_stale'] = finding['stale']
    result = []
    for _, entry in sorted(pairs.items()):
        stale = entry.pop('current_stale', False)
        target_stale = entry.pop('target_stale', False)
        result.append(assess(entry, snapshot, now, stale=stale, target_stale=target_stale, resolver=resolver))
    return result
