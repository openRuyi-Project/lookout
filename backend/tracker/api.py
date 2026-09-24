"""Read-only REST. HTTP processes never import or invoke the collector."""
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Literal
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from . import state, view, package_list, monitor_views

# Fixed-shape payloads are typed so the response contract cannot silently drift.
# Raw provenance (source, upstream, per-flavor facts) stays open on purpose.
class Target(BaseModel):
    id: str
    label: str
    repository: str
    architecture: str

class LastSuccess(BaseModel):
    version: str | None
    time: str
    srcmd5: str | None

class Build(BaseModel):
    target: str
    label: str
    repository: str
    architecture: str
    raw_status: str
    text: str
    kind: str
    issue: bool
    log_url: str | None
    stale: bool
    matches_source: bool | None
    last_success: LastSuccess | None
    updated_at: str | None

class BuildFlavor(BaseModel):
    model_config = ConfigDict(extra='allow')  # raw OBS provenance remains available
    package: str
    raw_status: str
    text: str
    kind: str
    issue: bool
    log_url: str | None
    stale: bool
    updated_at: str | None
    matches_source: bool | None
    last_success: LastSuccess | None

class BuildDetail(Build):
    flavors: list[BuildFlavor]

class Collection(BaseModel):
    obs_updated_at: str | None
    upstream_updated_at: str | None
    last_attempt: str | None
    mode: str
    errors: list[str]
    generation: int
    packages: int
    tracked_packages: int

class SpecMetadata(BaseModel):
    name: str | None
    version: str | None
    summary: str | None
    license: str | None
    url: str | None
    description: str | None
    buildsystem: str | None = None

class Appearance(BaseModel):
    background: str
    foreground: str

class Presentation(BaseModel):
    buildsystems: dict[str, Appearance] = {}

class MaintenanceLabel(BaseModel):
    label: str
    count: int
    stale: bool

class Evidence(BaseModel):
    key: str
    code: str | None = None
    value: str | bool | int | float | list[str] | None
    source: str
    url: str
    status: Literal['observed', 'unavailable', 'not_applicable', 'not_evaluated']

class Finding(BaseModel):
    id: str
    label: str
    title: str
    facts: list[Evidence]
    evidence_url: str
    scope: Literal['current', 'upgrade']
    tags: list[str]
    target_version: str | None
    monitor: str
    stale: bool

class MonitorCheck(BaseModel):
    changed_at: str | None = None
    evidence_revision: str | None = None
    monitor: str
    status: str
    checked_at: str | None
    attempted_at: str | None
    note: str | None
    error: str | None

class ChangelogEntry(BaseModel):
    commit: str
    date: str
    author: str
    subject: str
    signed_off_by: list[str]

class Spec(BaseModel):
    source_path: str
    source_url: str | None
    metadata: SpecMetadata | None
    changelog: list[ChangelogEntry]
    head: str | None
    error: str | None

class PackageSummary(BaseModel):
    name: str
    current: str | None
    current_build_success: bool | None
    last_successful_version: str | None
    upstream_updated_at: str | None
    latest: str | None
    relation: Literal['current', 'outdated', 'ahead', 'unknown', 'untracked', 'not_applicable']
    track: str | None
    track_label: str
    stale: bool
    needs_attention: bool
    builds: list[Build]
    detail_url: str
    buildsystem: str | None
    buildsystem_status: Literal['declared', 'not_declared', 'unknown']
    maintenance: list[MaintenanceLabel]

class Watch(BaseModel):
    model_config = ConfigDict(extra='allow')
    id: str
    version: str | None = None
    error: str | None = None
    fetched_at: str | None = None
    stale: bool

class PackageDetail(PackageSummary):
    builds: list[BuildDetail]
    source: dict
    upstream: dict
    watch: list[Watch]
    version_error: str | None
    last_known_relation: str
    spec: Spec
    maintenance_findings: list[Finding]
    monitor_checks: list[MonitorCheck]
    presentation: Presentation

class BuildStatusOption(BaseModel):
    value: str
    label: str
    count: int


class PackageList(BaseModel):
    items: list[PackageSummary]
    total: int
    page: int
    per_page: int
    pages: int
    counts: dict[str, int]
    targets: list[Target]
    collection: Collection
    presentation: Presentation
    buildsystems: dict[str, int]
    maintenance_labels: dict[str, int]
    build_statuses: dict[str, list[BuildStatusOption]]


# v2 is the uniform monitor read model. v1 below is only a field-name adapter.
class ObservationCheck(BaseModel):
    status: str
    stale: bool
    checked_at: str | None = None
    attempted_at: str | None = None
    error: str | None = None
    note: str | None = None
    changed_at: str | None = None
    evidence_revision: str | None = None


class SourceSummary(BaseModel):
    kind: Literal['source']
    version: str | None
    buildsystem: str | None
    buildsystem_status: Literal['declared', 'not_declared', 'unknown']


class SourceObservation(SourceSummary, Spec):
    revision: str | None
    obs: dict


class VersionSummary(BaseModel):
    kind: Literal['version']
    current: str | None
    latest: str | None
    relation: Literal['current', 'outdated', 'ahead', 'unknown', 'untracked', 'not_applicable']
    track: str | None
    track_label: str
    stale: bool
    error: str | None
    last_known_relation: str
    updated_at: str | None


class VersionObservation(VersionSummary):
    upstream: dict
    watch: list[Watch]


class BuildSummary(BaseModel):
    kind: Literal['build']
    targets: list[Build]
    source_version: str | None
    source_success: bool | None
    last_successful_version: str | None


class BuildObservation(BuildSummary):
    targets: list[BuildDetail]


class EvidenceEntry(BaseModel):
    id: str
    title: str
    evidence_url: str
    stale: bool


class EvidenceSummary(BaseModel):
    kind: Literal['evidence']
    labels: list[MaintenanceLabel]
    finding_count: int
    entries: list[EvidenceEntry] = []


class EvidenceObservation(EvidenceSummary):
    findings: list[Finding]


class MonitorDescription(BaseModel):
    id: str
    title: str
    kind: Literal['source', 'version', 'build', 'evidence']


class MonitorSummary(BaseModel):
    id: str
    title: str
    check: ObservationCheck
    data: SourceSummary | VersionSummary | BuildSummary | EvidenceSummary


class MonitorObservation(BaseModel):
    id: str
    title: str
    check: ObservationCheck
    data: SourceObservation | VersionObservation | BuildObservation | EvidenceObservation


class MonitoredPackage(BaseModel):
    name: str
    detail_url: str
    monitors: dict[str, MonitorSummary]


class MonitoredDetail(MonitoredPackage):
    monitors: dict[str, MonitorObservation]
    presentation: Presentation


class MonitoredList(BaseModel):
    items: list[MonitoredPackage]
    monitors: list[MonitorDescription]
    total: int
    page: int
    per_page: int
    pages: int
    counts: dict[str, int]
    targets: list[Target]
    collection: Collection
    presentation: Presentation
    buildsystems: dict[str, int]
    maintenance_labels: dict[str, int]
    build_statuses: dict[str, list[BuildStatusOption]]
    check_statuses: dict[str, int]
    section: Literal['results', 'coverage']
    result_count: int
    coverage_count: int


def create_app(db=None):
    db = Path(db or os.environ.get('TRACKER_DB', 'state/tracker.sqlite3'))
    app = FastAPI(title='openRuyi Package Monitor', version='0.1.0', docs_url=None, redoc_url=None,
                  description='Read-only collected facts. succeeded is not a release or revision verification claim.')
    cache, lock = {}, threading.Lock()
    def data():
        try:
            # One critical section binds a snapshot and its projection. A request
            # must never pair rows from a newer snapshot with older scope/targets,
            # even when a rewritten database retains the same generation number.
            with lock:
                st = db.stat() if db.exists() else None
                signature = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size) if st else None
                clock_changed = False
                if 'snapshot' not in cache or cache.get('signature') != signature:
                    same_file = signature is not None and cache.get('signature') is not None and signature[:2] == cache['signature'][:2]
                    previous = (cache['snapshot'], cache.get('revision')) if same_file else None
                    snap, revision = state.read_cached(db, previous)
                    clock_changed = bool(previous and revision is not None and revision == cache.get('revision'))
                    if clock_changed:
                        before = cache['snapshot']['components'].get('builds', {}).get('fetched_at')
                        after = snap['components'].get('builds', {}).get('fetched_at')
                        # Restoring an older backup can rewind the clock without
                        # changing its payload revision. Re-evaluate freshness.
                        clock_changed = bool(before and after and
                            datetime.fromisoformat(after) >= datetime.fromisoformat(before))
                    if not clock_changed:
                        cache.clear()
                    cache.update(snapshot=snap, revision=revision, signature=signature)
                snap = cache['snapshot']
                if not snap['generation']:
                    raise HTTPException(503, 'No collected snapshot yet')
                now = datetime.fromtimestamp(time.time(), timezone.utc)
                deadline = cache.get('deadline')
                if ('projected' not in cache or now < cache['last_seen']
                        or (deadline is not None and now >= deadline)):
                    cache['projected'] = view.project_monitors(snap, now)
                    cache['deadline'] = view.next_transition(snap, now)
                elif clock_changed:
                    cache['projected'] = view.refresh_build_clock(snap, cache['projected'][0], now)
                    cache['deadline'] = view.next_transition(snap, now)
                # Check against the latest request, not just the projection time:
                # a clock reversal can make an expired/future observation valid.
                cache['last_seen'] = now
                rows, collection = cache['projected']
                return snap, rows, collection
        except (OSError, sqlite3.Error, ValueError):
            raise HTTPException(503, 'Snapshot unavailable') from None
    @app.get('/healthz')
    def health():
        return {'status': 'ok'}
    @app.get('/readyz')
    def ready():
        snap, rows, collection = data()
        return {'status': 'degraded' if collection['errors'] else 'ready', 'packages': len(rows),
                'generation': snap['generation'], 'last_attempt': snap['last_attempt']}
    @app.get('/api/v1/packages', response_model=PackageList)
    def packages(q: str = Query('', max_length=100), view_name: Literal['all', 'updates', 'problems', 'attention', 'untracked'] = Query('all', alias='view'),
                 page: int = Query(1, ge=1, le=1000000), per_page: int = Query(100, ge=1, le=200),
                 buildsystem: str = Query('', max_length=100), maintenance: str = Query('', max_length=40),
                 build: list[str] = Query(default=[], max_length=16)):
        snap, rows, collection = data()
        try:
            builds = package_list.build_selections(build, snap['targets'])
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        result = package_list.PackageList(rows, snap['targets'], q).select(
            view=view_name, buildsystem=buildsystem, maintenance=maintenance,
            builds=builds, page=page, per_page=per_page,
        )
        result['items'] = [view.summary(view.legacy_package(row)) for row in result['items']]
        return {**result, 'targets': snap['targets'], 'collection': collection,
                'presentation': snap.get('presentation', {})}
    def find_package(name, rows):
        found = next((r for r in rows if r['name'] == name), None)
        if found is None:
            raise HTTPException(404, 'Package not found')
        return found
    @app.get('/api/v1/packages/{name}', response_model=PackageDetail)
    def package(name: str):
        snap, rows, _ = data()
        return {**view.legacy_package(find_package(name, rows)), 'presentation': snap.get('presentation', {})}
    @app.get('/api/v2/packages', response_model=MonitoredList)
    def monitor_packages(q: str = Query('', max_length=100),
                         view_name: Literal['all', 'updates', 'problems', 'attention', 'untracked'] = Query('all', alias='view'),
                         page: int = Query(1, ge=1, le=1000000), per_page: int = Query(100, ge=1, le=200),
                         buildsystem: str = Query('', max_length=100), maintenance: str = Query('', max_length=40),
                         build: list[str] = Query(default=[], max_length=16),
                         monitor: str = Query('', max_length=64), check: str = Query('', max_length=40),
                         section: Literal['results', 'coverage'] = Query('coverage')):
        snap, rows, collection = data()
        catalog = [module.describe() for module in monitor_views.registry(snap)]
        if monitor and monitor not in {m['id'] for m in catalog}:
            raise HTTPException(422, 'Unknown monitor')
        if check and not monitor:
            raise HTTPException(422, 'Check status requires a monitor')
        # Existing API clients retain the all-package default. The website asks
        # explicitly for results; old check links always enter the coverage view.
        section = 'coverage' if check else section
        focused = next((m for m in catalog if m['id'] == monitor), None)
        try:
            builds = package_list.build_selections(build, snap['targets'])
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        result = package_list.PackageList(rows, snap['targets'], q, monitor=monitor).select(
            view=view_name, buildsystem=buildsystem, maintenance=maintenance,
            builds=builds, page=page, per_page=per_page, check=check,
            findings_only=bool(focused and focused['kind'] == 'evidence' and section == 'results'))
        return {**result, 'items': [view.monitor_summary(row, monitor) for row in result['items']],
                'section': section,
                'monitors': catalog, 'targets': snap['targets'], 'collection': collection,
                'presentation': snap.get('presentation', {})}

    @app.get('/api/v2/packages/{name}', response_model=MonitoredDetail)
    def monitor_package(name: str):
        snap, rows, _ = data()
        return {**find_package(name, rows), 'presentation': snap.get('presentation', {})}

    @app.get('/api/v1/tracks/{track_id}')
    def track(track_id: str):
        snap, _, _ = data()
        if track_id not in snap['tracks']:
            raise HTTPException(404, 'Track not found')
        fact = snap['tracks'][track_id]
        return dict(id=track_id, **fact, stale=state.stale(fact, datetime.now(timezone.utc), snap.get('stale_after_seconds', 86400)))
    @app.get('/api/v1/presentation', response_model=Presentation)
    def presentation():
        snap, _, _ = data()
        return snap.get('presentation', {})
    @app.get('/api/v1/targets', response_model=list[Target])
    def targets():
        snap, _, _ = data()
        return snap['targets']
    @app.get('/api/v1/status')
    def status():
        snap, rows, collection = data()
        coverage = {}
        for row in rows:
            for mid, module in row['monitors'].items():
                counts = coverage.setdefault(mid, {})
                status = module['check']['status']
                counts[status] = counts.get(status, 0) + 1
        rows = [view.legacy_package(row) for row in rows]
        return {**collection, 'packages': len(rows), 'source_versions': sum(bool(r['current']) for r in rows),
                'tracked_packages': sum(bool(r['track']) for r in rows), 'components': snap['components'],
                'monitor_coverage': coverage, 'upstream_failures': view.upstream_failures(rows)}
    @app.get('/api/v1/export')
    def export():
        snap, rows, collection = data()
        return JSONResponse({'schema': 1, 'collection': collection, 'targets': snap['targets'], 'packages': [view.legacy_package(row) for row in rows]},
                            headers={'Content-Disposition': 'attachment; filename="openruyi-packages.json"'})
    @app.middleware('http')
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    return app

app = create_app()
