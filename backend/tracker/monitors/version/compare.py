"""One version decision from saved observations, shared by readers and monitors."""
from dataclasses import dataclass
from datetime import datetime, timezone

from tracker import config as cfg, identity as package_identity, state
from tracker.monitors.source import release as source_release


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
    release: source_release.Release | None = None
    revision: source_release.Revision | None = None

    @property
    def identity_conflict(self):
        identity = package_identity.from_native(self.upstream.get('source') or {})
        return bool(self.release and identity and self.release.identity != identity)

    @property
    def subject(self):
        return {'name': self.name, 'version': self.release.version if self.release else self.source.get('version'),
                'revision': self.source.get('revision')}

    @property
    def upgrading(self):
        return self.relation == 'outdated'

    @property
    def target_version(self):
        entry = self.upstream.get('source') or {}
        if source_release.tracks_commits(entry):
            return None
        return self.upstream.get('version')

    @property
    def stale(self):
        return (bool(self.source.get('version') and (self.source_stale or self.source.get('error')))
                or bool(self.upstream.get('version') and (self.upstream_stale or self.upstream.get('error'))))


def evaluate(snapshot, name, now=None, *, native_ids=None):
    now = now or datetime.now(timezone.utc)
    binding = cfg.resolve_binding(name, native_ids if native_ids is not None else snapshot.get('native_ids', ()),
                                  snapshot.get('bindings', {}).get(name, {}))
    source = state.current_source(snapshot, name)
    release = source_release.from_source(source)
    track = binding['compare']
    upstream = snapshot.get('tracks', {}).get(track, {}) if track else {}
    entry = upstream.get('source') or {}
    commit_mode = source_release.tracks_commits(entry)
    revision = source_release.revision(source, entry) if commit_mode else None
    source_stale = state.stale(source, now, source['stale_after_seconds'])
    upstream_stale = state.stale(upstream, now, snapshot.get('stale_after_seconds', 86400)) if track else False
    # Historical comparison is retained for API evidence, never upgrade eligibility.
    current, target = source.get('version'), upstream.get('version')
    if release:
        # Main comparison selects formal releases. A prerelease with the same
        # numeric base is still older; RPM's display version may have lost that
        # suffix. Keep native RPM comparison for the numeric release versions.
        released = source_release.semver(release.version)
        wanted = source_release.semver(target)
        current = released['base']
        target = wanted['base'] if wanted and not wanted['preview'] else None
    if commit_mode:
        target = source_release.observed_commit(upstream)
        last = ('current' if revision.current == target else 'changed') if (
            revision and source_release.commit_hash(target) and binding.get('comparable', True)) else 'unknown'
    else:
        last = state.compare(current, target, binding.get('comparable', True))
    if release and last == 'current' and released['preview']:
        last = 'outdated'
    identity = package_identity.from_native(upstream.get('source') or {})
    conflict = bool(release and identity and release.identity != identity)
    if conflict:
        last = 'unknown'
    error = None
    if not source.get('version') or source.get('error') or source_stale:
        relation = 'unknown'
        error = source.get('error') or source.get('version_error') or 'source version unknown or stale'
    elif conflict:
        relation, error = 'unknown', 'Source0 release identity differs from the configured upstream identity'
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
            error = ('Source0 commit and tracked branch cannot be reliably compared' if commit_mode
                     else 'versions not reliably comparable with native RPM')
    return VersionStatus(name, binding, source, track, upstream, source_stale,
                         upstream_stale, relation, last, error, release, revision)


def evaluate_all(snapshot, now=None):
    """Resolve each package once per read/collection pass; share the track-ID set."""
    now = now or datetime.now(timezone.utc)
    native_ids = frozenset(snapshot.get('native_ids', ()))
    return {name: evaluate(snapshot, name, now, native_ids=native_ids)
            for name in snapshot.get('sources', {})}
