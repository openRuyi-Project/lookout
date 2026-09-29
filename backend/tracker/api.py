"""Read-only REST. HTTP processes never import or invoke the collector."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import threading
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from tracker import state
from tracker.monitors.model import RawFinding
from tracker.monitors.requires.model import RequirementAssessment
from tracker.presentation import navigation as presentation_navigation, pages as presentation_pages
from tracker.presentation.model import DetailDocument, DocumentTheme, ListingDocument
from tracker.readmodel import monitors as monitor_views, packages as package_list, snapshot as view
from tracker.readmodel.cache import ProjectionCache

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

class SourceRepository(BaseModel):
    url: str
    branch: str
    revision: str


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
    build_service_url: str | None = None
    source_repository: SourceRepository | None = None

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
    icon: str | None = None

class Presentation(BaseModel):
    buildsystems: dict[str, Appearance] = {}

class MaintenanceLabel(BaseModel):
    label: str
    count: int
    stale: bool

class Finding(RawFinding):
    monitor: str
    stale: bool


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


class Watch(BaseModel):
    model_config = ConfigDict(extra='allow')
    id: str
    version: str | None = None
    error: str | None = None
    fetched_at: str | None = None
    stale: bool


class BuildStatusOption(BaseModel):
    value: str
    label: str
    count: int


# Facts and display documents are separate projections of the same snapshot.
class ObservationCheck(BaseModel):
    status: str
    stale: bool
    checked_at: str | None = None
    attempted_at: str | None = None
    error: str | None = None
    note: str | None = None
    changed_at: str | None = None
    evidence_revision: str | None = None
    failures: list[str] = Field(default_factory=list, description='Failed collection subchecks; partial evidence remains usable.')


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


class MonitoredObservation(MonitoredPackage):
    monitors: dict[str, MonitorObservation]


class MonitoredDetail(MonitoredObservation):
    presentation: Presentation


class MonitoredList(BaseModel):
    items: list[MonitoredPackage | MonitoredObservation]
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
    check_groups: dict[str, int]
    section: Literal['results', 'coverage']
    result_count: int
    coverage_count: int
    retained_count: int


class MonitorExport(BaseModel):
    schema_version: Literal[2] = Field(2, alias='schema')
    collection: Collection
    targets: list[Target]
    packages: list[MonitoredObservation]


class PackageBatch(BaseModel):
    items: list[MonitoredObservation]
    collection: Collection


class ListingQuery(BaseModel):
    q: str = Field('', max_length=100)
    view: Literal['all', 'updates', 'problems', 'attention', 'untracked'] = 'all'
    page: int = Field(1, ge=1, le=1000000)
    per_page: int = Field(100, ge=1, le=200)
    buildsystem: str = Field('', max_length=100)
    maintenance: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(
        default_factory=list, max_length=16,
        description='Repeated issue labels; all must match. Counts retain selected labels when adding another.')
    requires: Literal['', 'unmet', 'changes'] = ''
    signal: str = Field('', max_length=64, description='Owning monitor ID of a Version annotation.')
    freshness: Literal['', 'retained'] = ''
    build: list[str] = Field(default_factory=list, max_length=16)
    monitor: str = Field('', max_length=64)
    check: str = Field('', max_length=40)
    section: Literal['results', 'coverage'] = 'results'


FULL_PAGE_LIMIT = 20
MonitorID = Annotated[str, Field(min_length=1, max_length=64)]
PackageName = Annotated[str, Field(min_length=1, max_length=512)]


class MonitorSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    include: list[MonitorID] = Field(default_factory=list, max_length=32,
        description='Repeated monitor IDs to return. Omitted means all; does not change filtering.')


class PackageQuery(ListingQuery, MonitorSelection):
    search: Literal['name', 'observations'] = Field('name',
        description='Case-insensitive substring search. observations searches values in the focused monitor, or all monitors when unfocused; package names always match.')
    detail: Literal['summary', 'full'] = Field('summary',
        description=f'Full observations include evidence and histories; require per_page <= {FULL_PAGE_LIMIT}.')


class BatchQuery(MonitorSelection):
    names: list[PackageName] = Field(min_length=1, max_length=FULL_PAGE_LIMIT,
        description=f'Repeated exact package names, in response order; 1–{FULL_PAGE_LIMIT} unique names. Any missing name fails the whole request.')


def selected_monitors(requested, catalog):
    known = {module['id'] for module in catalog}
    if set(requested) - known:
        raise HTTPException(422, 'Unknown included monitor')
    return set(requested) if requested else known


def package_response(row, included, *, full, focus=''):
    selected = {**row, 'monitors': {mid: result for mid, result in row['monitors'].items() if mid in included}}
    if full:
        return MonitoredObservation.model_validate(selected)
    return MonitoredPackage.model_validate(view.monitor_summary(selected, focus))


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

    app = FastAPI(title='openRuyi Lookout', version='0.1.0', docs_url=None, redoc_url=None,
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
    def find_package(name, index):
        found = index.by_name.get(name)
        if found is None:
            raise HTTPException(404, 'Package not found')
        return found
    def select_monitored(filters, *, document=False, search='name'):
        snap, index, collection = data()
        catalog = [module.describe() for module in monitor_views.registry(snap)]
        if filters.monitor and filters.monitor not in {m['id'] for m in catalog}:
            raise HTTPException(422, 'Unknown monitor')
        if document:
            filters = ListingQuery(**presentation_navigation.listing_query(filters.model_dump(), catalog))
        q, view_name = filters.q, filters.view
        page, per_page = filters.page, filters.per_page
        buildsystem, maintenance, build = filters.buildsystem, filters.maintenance, filters.build
        monitor, check = filters.monitor, filters.check
        section = filters.section
        if check and not monitor:
            raise HTTPException(422, 'Check status requires a monitor')
        if filters.freshness and not monitor:
            raise HTTPException(422, 'Freshness requires a monitor')
        # Check filters describe collection coverage, not positive findings.
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
            search=search,
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
                    ('counts', 'requires_counts', 'version_signals', 'check_statuses', 'check_groups', 'result_count', 'retained_count')}
        return {**result,
                'section': section,
                'monitors': catalog, 'targets': snap['targets'], 'collection': collection,
                'presentation': snap.get('presentation', {})}

    @app.get('/api/v2/packages', response_model=MonitoredList)
    def monitor_packages(filters: Annotated[PackageQuery, Query()]):
        if filters.detail == 'full' and filters.per_page > FULL_PAGE_LIMIT:
            raise HTTPException(422, f'Full observations require per_page <= {FULL_PAGE_LIMIT}')
        result = select_monitored(filters, search=filters.search)
        included = selected_monitors(filters.include, result['monitors'])
        return {**result, 'items': [package_response(row, included,
            full=filters.detail == 'full', focus=filters.monitor) for row in result['items']]}

    @app.get('/api/ui/packages', response_model=ListingDocument)
    def listing_document(filters: Annotated[ListingQuery, Query()]):
        result = select_monitored(filters, document=True)
        return presentation_pages.listing(result, result['query'])

    @app.get('/api/v2/packages:batchGet', response_model=PackageBatch)
    def batch_packages(filters: Annotated[BatchQuery, Query()]):
        if len(set(filters.names)) != len(filters.names):
            raise HTTPException(422, 'Batch package names must be unique')
        snap, index, collection = data()
        included = selected_monitors(filters.include, [module.describe() for module in monitor_views.registry(snap)])
        rows = [find_package(name, index) for name in filters.names]
        return {'items': [package_response(row, included, full=True) for row in rows], 'collection': collection}

    @app.get('/api/v2/packages/{name}', response_model=MonitoredDetail)
    def monitor_package(name: str, filters: Annotated[MonitorSelection, Query()]):
        snap, index, _ = data()
        included = selected_monitors(filters.include, [module.describe() for module in monitor_views.registry(snap)])
        package = package_response(find_package(name, index), included, full=True)
        # Keep validated submodels; only the HTTP boundary serializes them.
        return MonitoredDetail(**dict(package), presentation=snap.get('presentation', {}))

    @app.get('/api/ui/packages/{name}', response_model=DetailDocument)
    def package_document(name: str):
        snap, index, collection = data()
        document = presentation_pages.detail(find_package(name, index))
        if notice := collection.get('projection_notice'):
            document.notices.append(notice)
        return document

    @app.get('/api/ui/theme', response_model=DocumentTheme)
    def document_theme():
        snap, _, _ = data()
        return presentation_pages.theme(snap.get('presentation', {}))

    @app.get('/api/v2/tracks/{track_id}')
    def track(track_id: str):
        snap, _, _ = data()
        if track_id not in snap['tracks']:
            raise HTTPException(404, 'Track not found')
        fact = snap['tracks'][track_id]
        return dict(id=track_id, **fact, stale=state.stale(fact, datetime.now(timezone.utc), snap.get('stale_after_seconds', 86400)))
    @app.get('/api/v2/targets', response_model=list[Target])
    def targets():
        snap, _, _ = data()
        return snap['targets']
    @app.get('/api/v2/status')
    def status():
        snap, index, collection = data()
        observations = ((row['name'], row['monitors']['version']['data']['upstream']) for row in index.rows)
        return {**collection,
                'source_versions': sum(bool(row['monitors']['source']['data']['version']) for row in index.rows),
                'components': snap['components'], 'monitor_coverage': index.monitor_coverage(),
                'upstream_failures': view.upstream_failures(observations)}
    @app.get('/api/v2/export', response_model=MonitorExport)
    def export(response: Response):
        snap, index, collection = data()
        response.headers['Content-Disposition'] = 'attachment; filename="openruyi-packages.json"'
        return {'schema': 2, 'collection': collection, 'targets': snap['targets'], 'packages': index.rows}
    @app.middleware('http')
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    return app

app = create_app()
