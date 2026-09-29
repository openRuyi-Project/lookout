"""Upstream dependency declarations and a separate distribution-source assessment."""
from dataclasses import dataclass
from functools import cached_property
from typing import Annotated, Literal

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from tracker import config as cfg, identity as package_identity, state
from tracker.monitors.observations import visible
from tracker.monitors.requires import compare as requirement_versions
from tracker.monitors.requires.markers import applies
from tracker.monitors.source import release as source_release


class Constraint(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expression: Annotated[str, Field(max_length=1024)]
    source: Annotated[str, Field(min_length=1, max_length=256)]
    url: Annotated[str, Field(min_length=1, max_length=8192)]


class DependencyIdentity(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    ecosystem: Annotated[str, Field(min_length=1, max_length=100)]
    name: Annotated[str, Field(min_length=1, max_length=512)]


class ProvidedComponent(BaseModel):
    """A component version reported for the enclosing exact upstream release."""
    model_config = ConfigDict(extra='forbid', strict=True)
    identity: DependencyIdentity
    version: Annotated[str, Field(min_length=1, max_length=200)]
    source: Annotated[str, Field(min_length=1, max_length=256)]
    url: Annotated[str, Field(min_length=1, max_length=8192)]


_PROVIDES = TypeAdapter(Annotated[list[ProvidedComponent], Field(max_length=4096)])


def validate_provides(values):
    parsed = _PROVIDES.validate_python(values)
    identities = [(item.identity.ecosystem, item.identity.name) for item in parsed]
    if len(identities) != len(set(identities)):
        raise ValueError('duplicate provided component identity')


class Dependency(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    dependency: Annotated[str, Field(pattern=r'^[a-z][a-z0-9_.-]{0,99}$')]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    kind: Literal['runtime', 'build']
    relationship: Literal['requires', 'recommends', 'suggests'] = 'requires'
    scheme: Annotated[str, Field(pattern=r'^[a-z][a-z0-9_-]{0,39}$')]
    version_scope: Literal['release', 'component'] = 'release'
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
    evidence_url: str | None = None


class RequirementAssessment(Dependency):
    current: Constraint | None
    target: Constraint | None
    package: str | None
    mapping: Literal['mapped', 'not_mapped', 'ambiguous', 'not_packaged']
    observed: DependencyObservation | None
    satisfaction: Literal['satisfied', 'unsatisfied', 'unknown', 'not_applicable']
    reason: str | None
    target_satisfaction: Literal['satisfied', 'unsatisfied', 'unknown', 'not_applicable'] = 'unknown'
    target_reason: str | None = None
    changed: bool = False


class UnsupportedRequirements(ValueError):
    """No reliable declaration can be recovered from this provider response."""


def unsatisfied(requirement):
    """Either observed release can conflict; unknown is never a conflict."""
    return 'unsatisfied' in (requirement['satisfaction'], requirement['target_satisfaction'])


@dataclass(frozen=True)
class Requirement:
    """A provider declaration with a parser-specific working value.

    comparison stays inside the backend (for example, to combine clauses).
    fact() persists declaration and scheme; the read-side comparator uses those,
    not the parser object or the collector's environment.
    """
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
    relationship: str = 'requires'
    version_scope: str = 'release'

    def fact(self):
        return dict(dependency=self.dependency, name=self.name, kind=self.kind, scheme=self.scheme,
                    version_scope=self.version_scope,
                    identity=self.identity, condition=self.condition, extras=list(self.extras), optional=self.optional,
                    relationship=self.relationship,
                    constraint=dict(expression=self.declaration, source=self.source, url=self.url))


def key(item):
    identity = item.get('identity') or {}
    return (item['dependency'], item['kind'], item['scheme'], item.get('condition') or '',
            tuple(item.get('extras', [])), identity.get('ecosystem', ''), identity.get('name', ''),
            item.get('relationship', 'requires'), item.get('version_scope', 'release'))


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
        self.identities, self.observations, self.components = {}, {}, {}
        self.packages = set(snapshot.get('sources', {})) | set(snapshot.get('inventory', {}).values())
        inventory = snapshot.get('components', {}).get('inventory', {})
        ttl = snapshot.get('obs_stale_after_seconds', snapshot.get('stale_after_seconds', 86400))
        self.inventory_current = not inventory.get('error') and not state.stale(inventory, now, ttl)
        ids = frozenset(snapshot.get('native_ids', ()))
        for package in snapshot.get('sources', {}):
            binding = cfg.resolve_binding(package, ids, snapshot.get('bindings', {}).get(package, {}))
            native = snapshot.get('tracks', {}).get(binding['compare'], {}).get('source') or {}
            release = source_release.from_source(state.current_source(snapshot, package))
            identity = package_identity.from_package({'identity': native,
                'source_release': release.public() if release else None})
            if identity:
                self.identities.setdefault(self.identity_key(identity), set()).add(package)
            self.index_components(package)

    def index_components(self, package):
        observation = visible(self.snapshot.get('monitors', {}).get(package, {}).get('requires', {}))
        check = observation.get('scope_checks', {}).get('current', {})
        provided = check.get('provides', [])
        if not provided:
            return
        source = state.current_source(self.snapshot, package)
        subject = observation.get('subject', {})
        if (observation.get('_retained') or check.get('status') != 'ok' or observation.get('input_status') == 'unsupported'
                or any(subject.get(k) != source.get(k) for k in ('version', 'revision'))
                or self.observed(package)['stale']
                or state.stale(check, self.now, self.snapshot.get('monitor_stale_after_seconds', 86400))):
            return
        try:
            validate_provides(provided)
        except (ValueError, TypeError):
            return
        for component in provided:
            identity = self.identity_key(component['identity'])
            self.identities.setdefault(identity, set()).add(package)
            self.components[package, identity] = dict(version=component['version'],
                revision=source['revision'], origin=component['source'], checked_at=check['checked_at'],
                stale=False, evidence_url=component['url'])

    @cached_property
    def environment(self):
        environment = dict(self.snapshot.get('dependency_environments', {}).get('pep508', {}))
        package = self.snapshot.get('dependency_packages', {}).get('python')
        if package:
            observed = self.observed(package)
            if not observed['stale'] and observed['revision']:
                try:
                    from packaging.version import Version
                    version = Version(observed['version'])
                    if len(version.release) >= 2:
                        environment['python_version'] = '.'.join(map(str, version.release[:2]))
                        environment['python_full_version'] = str(version)
                except (ValueError, TypeError):
                    pass
        return environment

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

    def observed(self, package, identity=None, *, component=False):
        if component and identity:
            provided = self.components.get((package, self.identity_key(identity)))
            if provided is not None:
                return provided
        if package not in self.observations:
            source = state.current_source(self.snapshot, package)
            expired = state.stale(source, self.now, source['stale_after_seconds'])
            self.observations[package] = dict(version=source.get('version'), revision=source.get('revision'),
                origin=source['origin'], checked_at=source.get('fetched_at'),
                stale=expired or bool(source.get('error')))
        observation = self.observations[package]
        # An explicit package mapping identifies the owner, not a subcomponent's version.
        return {**observation, 'version': None} if component else observation


def assess(change, snapshot, now, *, stale=False, target_stale=False, resolver=None):
    """Marks concern the declared constraint and source VERSION, not installability."""
    resolver = resolver or Resolver(snapshot, now)
    package, mapping = resolver.resolve(change)
    observed = (resolver.observed(package, change.get('identity'), component=change.get('version_scope') == 'component')
                if mapping == 'mapped' else None)

    def evaluate(constraint, unavailable):
        if constraint is None:
            return 'unknown', 'requirement_not_observed'
        if unavailable:
            return 'unknown', 'requirement_unavailable'
        applicable = (applies(change.get('condition'), resolver.environment)
                      if change['scheme'] == 'pep440' else None if change.get('condition') else True)
        if applicable is False:
            return 'not_applicable', 'condition_false'
        if applicable is None or change.get('extras'):
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
