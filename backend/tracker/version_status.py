"""One version decision from saved observations, shared by readers and monitors."""
from dataclasses import dataclass
from datetime import datetime, timezone

from . import config as cfg, state


@dataclass(frozen=True)
class VersionStatus:
    name: str
    binding: dict
    source: dict
    track: str | None
    upstream: dict
    source_stale: bool
    upstream_stale: bool
    relation: str
    last_known_relation: str
    error: str | None

    @property
    def subject(self):
        return {'name': self.name, 'version': self.source.get('version'),
                'revision': self.source.get('revision')}

    @property
    def upgrading(self):
        return self.relation == 'outdated'

    @property
    def stale(self):
        return (bool(self.source.get('version') and (self.source_stale or self.source.get('error')))
                or bool(self.upstream.get('version') and (self.upstream_stale or self.upstream.get('error'))))


def evaluate(snapshot, name, now=None, *, native_ids=None):
    now = now or datetime.now(timezone.utc)
    binding = cfg.resolve_binding(name, native_ids if native_ids is not None else snapshot.get('native_ids', ()),
                                  snapshot.get('bindings', {}).get(name, {}))
    source = state.current_source(snapshot, name)
    track = binding['compare']
    upstream = snapshot.get('tracks', {}).get(track, {}) if track else {}
    source_stale = state.stale(source, now, source['stale_after_seconds'])
    upstream_stale = state.stale(upstream, now, snapshot.get('stale_after_seconds', 86400)) if track else False
    # Historical comparison is retained for API evidence, never upgrade eligibility.
    last = state.compare(source.get('version'), upstream.get('version'), binding.get('comparable', True))
    error = None
    if not source.get('version') or source.get('error') or source_stale:
        relation = 'unknown'
        error = source.get('error') or source.get('version_error') or 'source version unknown or stale'
    elif binding.get('not_applicable'):
        relation = 'not_applicable'
    elif not track:
        relation = 'untracked'
    elif upstream.get('error') or upstream_stale:
        relation = 'unknown'
        error = upstream.get('error') or 'upstream observation stale or missing'
    else:
        relation = last
        if relation == 'unknown':
            error = 'versions not reliably comparable with native RPM'
    return VersionStatus(name, binding, source, track, upstream, source_stale,
                         upstream_stale, relation, last, error)


def evaluate_all(snapshot, now=None):
    """Resolve each package once per read/collection pass; share the track-ID set."""
    now = now or datetime.now(timezone.utc)
    native_ids = frozenset(snapshot.get('native_ids', ()))
    return {name: evaluate(snapshot, name, now, native_ids=native_ids)
            for name in snapshot.get('sources', {})}
