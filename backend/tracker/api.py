"""Read-only REST. HTTP processes never import or invoke the collector."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import threading
from typing import Annotated, Literal
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from . import state, view, package_list, monitor_views, presentation as documents
from .presentation_model import ListingDocument, DetailDocument, DocumentTheme
from .read_model import ProjectionCache
from .monitor_model import RawFinding
from .requirements import RequirementAssessment

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
    projection_notice: str | None = None

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

class Finding(RawFinding):
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
    relation: Literal['current', 'outdated', 'changed', 'ahead', 'unknown', 'untracked', 'not_applicable']
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


class VersionAnnotation(BaseModel):
    monitor: str = Field(description='Stable owning monitor ID; also the version signal filter value.')
    label: str
    count: int = Field(ge=1, description='Distinct related findings, or changed dependency declarations.')
    scope: Literal['current', 'upgrade']
    target_version: str | None
    stale: bool
    finding_ids: list[str] = Field(description='IDs in the owning monitor observation; evidence is not duplicated.')


class SourceRelease(BaseModel):
    ecosystem: str
    name: str
    version: str
    url: str


class RevisionComparison(BaseModel):
    repository: str
    branch: str
    current: str
    packaged_date: str | None
    latest: str | None
    latest_committed_at: str | None
    links: dict[str, str | None]


class VersionSummary(BaseModel):
    kind: Literal['version']
    current: str | None
    source_release: SourceRelease | None = None
    revision: RevisionComparison | None = None
    latest: str | None
    relation: Literal['current', 'outdated', 'changed', 'ahead', 'unknown', 'untracked', 'not_applicable']
    track: str | None
    track_label: str
    stale: bool
    error: str | None
    last_known_relation: str
    updated_at: str | None
    annotations: list[VersionAnnotation] = []


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
    scope: Literal['current', 'upgrade']
    target_version: str | None = None
    tags: list[str] = []


class EvidenceSummary(BaseModel):
    kind: Literal['evidence']
    labels: list[MaintenanceLabel]
    finding_count: int
    entries: list[EvidenceEntry] = []


class EvidenceObservation(EvidenceSummary):
    findings: list[Finding]


class RequiresSummary(BaseModel):
    kind: Literal['requires']
    labels: list[MaintenanceLabel]
    finding_count: int
    current_version: str | None
    target_version: str | None
    requirements: list[RequirementAssessment]


class RequiresObservation(RequiresSummary):
    findings: list[Finding]


class MonitorDescription(BaseModel):
    id: str
    title: str
    kind: Literal['source', 'version', 'build', 'evidence', 'requires']


class MonitorSummary(BaseModel):
    id: str
    title: str
    check: ObservationCheck
    data: Annotated[SourceSummary | VersionSummary | BuildSummary | EvidenceSummary | RequiresSummary, Field(discriminator='kind')]


class MonitorObservation(BaseModel):
    id: str
    title: str
    check: ObservationCheck
    data: Annotated[SourceObservation | VersionObservation | BuildObservation | EvidenceObservation | RequiresObservation, Field(discriminator='kind')]


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
    requires_counts: dict[str, int]
    version_signals: dict[str, int]
    build_statuses: dict[str, list[BuildStatusOption]]
    check_statuses: dict[str, int]
    section: Literal['results', 'coverage']
    result_count: int
    coverage_count: int
    retained_count: int


class ListingQuery(BaseModel):
    q: str = Field('', max_length=100)
    view: Literal['all', 'updates', 'problems', 'attention', 'untracked'] = 'all'
    page: int = Field(1, ge=1, le=1000000)
    per_page: int = Field(100, ge=1, le=200)
    buildsystem: str = Field('', max_length=100)
    maintenance: str = Field('', max_length=40)
    requires: Literal['', 'unmet', 'changes'] = ''
    signal: str = Field('', max_length=64, description='Owning monitor ID of a Version annotation.')
    freshness: Literal['', 'retained'] = ''
    build: list[str] = Field(default_factory=list, max_length=16)
    monitor: str = Field('', max_length=64)
    check: str = Field('', max_length=40)
    section: Literal['results', 'coverage'] | None = None


def create_app(db=None):
    db = Path(db or os.environ.get('TRACKER_DB', 'state/tracker.sqlite3'))
    cache = ProjectionCache(db)

    @asynccontextmanager
    async def lifespan(app):
        worker = threading.Thread(target=cache.run, name='snapshot-projection', daemon=True)
        worker.start()
        try:
            yield
        finally:
            cache.stop()
            worker.join(timeout=15)

    app = FastAPI(title='openRuyi Package Monitor', version='0.1.0', docs_url=None, redoc_url=None,
                  lifespan=lifespan,
                  description='Read-only collected facts. succeeded is not a release or revision verification claim.')
    app.state.projection = cache

    def data():
        try:
            return cache.read()
        except ValueError:
            raise HTTPException(503, 'No prepared snapshot yet') from None
    @app.get('/healthz')
    def health():
        return {'status': 'ok'}
    @app.get('/readyz')
    def ready():
        snap, index, collection = data()
        return {'status': 'degraded' if collection['errors'] else 'ready', 'packages': len(index.rows),
                'generation': snap['generation'], 'last_attempt': snap['last_attempt'],
                'projection_notice': collection.get('projection_notice')}
    @app.get('/api/v1/packages', response_model=PackageList)
    def packages(q: str = Query('', max_length=100), view_name: Literal['all', 'updates', 'problems', 'attention', 'untracked'] = Query('all', alias='view'),
                 page: int = Query(1, ge=1, le=1000000), per_page: int = Query(100, ge=1, le=200),
                 buildsystem: str = Query('', max_length=100), maintenance: str = Query('', max_length=40),
                 build: list[str] = Query(default=[], max_length=16)):
        snap, index, collection = data()
        try:
            builds = package_list.build_selections(build, snap['targets'])
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        result = index.select(query=q,
            view=view_name, buildsystem=buildsystem, maintenance=maintenance,
            builds=builds, page=page, per_page=per_page,
        )
        result['items'] = [view.summary(view.legacy_package(row)) for row in result['items']]
        return {**result, 'targets': snap['targets'], 'collection': collection,
                'presentation': snap.get('presentation', {})}
    def find_package(name, index):
        found = index.by_name.get(name)
        if found is None:
            raise HTTPException(404, 'Package not found')
        return found
    @app.get('/api/v1/packages/{name}', response_model=PackageDetail)
    def package(name: str):
        snap, index, _ = data()
        return {**view.legacy_package(find_package(name, index)), 'presentation': snap.get('presentation', {})}
    def select_monitored(filters, default_section, *, document=False):
        snap, index, collection = data()
        catalog = [module.describe() for module in monitor_views.registry(snap)]
        if filters.monitor and filters.monitor not in {m['id'] for m in catalog}:
            raise HTTPException(422, 'Unknown monitor')
        if document:
            filters = ListingQuery(**documents.listing_query(filters.model_dump(), catalog))
        q, view_name = filters.q, filters.view
        page, per_page = filters.page, filters.per_page
        buildsystem, maintenance, build = filters.buildsystem, filters.maintenance, filters.build
        monitor, check = filters.monitor, filters.check
        section = filters.section or default_section
        if check and not monitor:
            raise HTTPException(422, 'Check status requires a monitor')
        if filters.freshness and not monitor:
            raise HTTPException(422, 'Freshness requires a monitor')
        # Existing API clients retain the all-package default. The website asks
        # explicitly for results; old check links always enter the coverage view.
        section = 'coverage' if check else section
        focused = next((m for m in catalog if m['id'] == monitor), None)
        try:
            builds = package_list.build_selections(build, snap['targets'])
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        result = index.select(query=q, monitor=monitor,
            view=view_name, buildsystem=buildsystem, maintenance=maintenance,
            builds=builds, page=page, per_page=per_page, check=check, requires=filters.requires, signal=filters.signal,
            freshness=filters.freshness,
            findings_only=bool(focused and focused['kind'] in ('evidence', 'requires') and section == 'results'))
        if document:
            result['query'] = {**filters.model_dump(), 'section': section, 'page': result['page']}
            if focused:
                # Peer tabs are alternatives. Count their destinations in the
                # shared search/identity scope, not inside the selected tab.
                navigation = index.select(query=q, monitor=monitor,
                    view='all', buildsystem=buildsystem, maintenance='', builds={},
                    page=1, per_page=1,
                    findings_only=focused['kind'] in ('evidence', 'requires'))
                result['navigation_counts'] = {key: navigation[key] for key in
                    ('counts', 'requires_counts', 'version_signals', 'check_statuses', 'result_count', 'retained_count')}
        return {**result,
                'section': section,
                'monitors': catalog, 'targets': snap['targets'], 'collection': collection,
                'presentation': snap.get('presentation', {})}

    @app.get('/api/v2/packages', response_model=MonitoredList)
    def monitor_packages(filters: Annotated[ListingQuery, Query()]):
        result = select_monitored(filters, 'coverage')
        return {**result, 'items': [view.monitor_summary(row, filters.monitor) for row in result['items']]}

    @app.get('/api/ui/packages', response_model=ListingDocument)
    def listing_document(filters: Annotated[ListingQuery, Query()]):
        result = select_monitored(filters, 'results', document=True)
        return documents.listing(result, result['query'])

    @app.get('/api/v2/packages/{name}', response_model=MonitoredDetail)
    def monitor_package(name: str):
        snap, index, _ = data()
        return {**find_package(name, index), 'presentation': snap.get('presentation', {})}

    @app.get('/api/ui/packages/{name}', response_model=DetailDocument)
    def package_document(name: str):
        snap, index, collection = data()
        document = documents.detail(find_package(name, index))
        if notice := collection.get('projection_notice'):
            document.notices.append(notice)
        return document

    @app.get('/api/ui/theme', response_model=DocumentTheme)
    def document_theme():
        snap, _, _ = data()
        return documents.theme(snap.get('presentation', {}))

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
        snap, index, collection = data()
        rows = index.rows
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
        snap, index, collection = data()
        return JSONResponse({'schema': 1, 'collection': collection, 'targets': snap['targets'], 'packages': [view.legacy_package(row) for row in index.rows]},
                            headers={'Content-Disposition': 'attachment; filename="openruyi-packages.json"'})
    @app.middleware('http')
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    return app

app = create_app()
