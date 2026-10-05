# Design and trust boundaries

## Ownership

The installer references version, identity and distribution catalogs in the image.
`/config` contains operating settings and explicit overrides. A native override replaces
one complete track. A package override replaces one policy field or complete monitor
identity. Publication and review checks include every parsed file. Observation keys
identify the effective rule or query, not its path or image revision. Unrelated catalog
changes preserve evidence. To distinguish defaults from operator edits, migration
requires the original installation baseline.

```text
configured identities → collectors → SQLite → readmodel → fact API
                                                ↓
                                          presentation → UI API → Astro
```

| Responsibility | Owner | Contract |
|---|---|---|
| Settings and catalogs | `config.py`, `catalog.py` | Explicit references, unit-level overrides, no observation-dependent loading |
| Version discovery | `monitors/version/` | Proposes candidates; reviewed native rules execute |
| External protocols | `monitors/`, `providers/` | Attributed facts, not maintainer decisions |
| Writes | `collector.py`, `state.py`, `storage.py` | Phase-owned fields, one serialized transaction |
| Derived facts and selection | `readmodel/` | Saved input only; no collection |
| Reading order and grouping | `presentation/` | Pure fields, tables, entries and links |
| Layout and interaction | `frontend/` | Renders documents, submits queries; no monitor inference |

Adapters register in `monitors/registry.py`. Readers use the saved catalog. Package
initializers do not run collection or registration. `scripts/check-architecture.py` and
Import Linter enforce import boundaries. `scripts/api-types.py --check` checks generated
types against OpenAPI. [Porting](monitor-porting.md) describes extension contracts.
[Configuration](../config/README.md) explains editing and promotion.

## Persistence and publication

Collectors fetch outside the writer lock, then merge onto the latest snapshot.
`state.merge` rejects fields outside a phase's ownership. A complete Git tree of regular
`SPECS/*/*.spec` files defines the package catalog. It includes packages whose SPEC
parsing failed and packages absent from OBS. If a refresh fails, Lookout retains the
confirmed catalog. OBS inventories describe build objects. They cannot add or remove
source packages. OBS status and history share an identity but not ownership:

| Phase | Writes |
|---|---|
| `builds` | Project-wide `_result` status, errors and poll timestamps |
| `obs` | Inventory, source identity and successful-build history |

`state.BUILD_FIELDS` prevents slow history responses from overwriting newer status. A
change to the target repository or architecture invalidates old evidence and pending
responses for that scope.

SQLite stores source, track, SPEC, build and monitor observations as keyed rows.
Transactions write changed rows, deletions and the snapshot header. Equal rows retain
their storage revision without JSON re-encoding. Incremental commits must match the
database revision and heartbeat they read. Storage rejects commits based on stale
inputs. `state.read_cached` captures header, clock and row revisions in one transaction,
then decodes changed JSON outside the lock. Borrowed unchanged data is read-only.

A complete successful OBS poll with identical facts updates `snapshot_clock`, not
content generation or all build records. Partial or failed polls cannot update
timestamps for missing observations this way. Independent phase merges preserve the
latest clock. Imports get a new storage identity, even when their content is unchanged.

Storage `user_version` is independent of public snapshot `schema`. Writers require the
current format. The migration tool backs up the database, migrates it in a transaction
and compares observations. It rejects unknown formats. Compatible image upgrades retain
data, query fingerprints and evidence. Operational commands and rollback limits are in
[Deployment](deployment.md#image-upgrades).

Writes use rollback journals and `synchronous=FULL`. API connections are read-only.
Startup recovery holds the writer lock while SQLite recovers a hot journal. It never
deletes journals or replaces corrupt data. Standalone preflight does not write an
existing database.

### Prepared reads

A background task checks storage once per second. It rebuilds the projection and index
when data changes, a freshness deadline expires or the clock moves backward. Fresh
clock-only updates reuse unchanged facets and monitor search text. Changed build
timestamps remain searchable. The task prepares status aggregates once per publication.
Refresh-failure notices remain live. Publication replaces the complete model atomically.
Concurrent readers retain the previous complete model.

Readiness fails until the first model is published. If a refresh fails or is overdue,
Lookout retains the last model, shows a notice and reports degraded readiness. Liveness
tests only the Node → FastAPI chain. Neither endpoint proves provider coverage.

The fact API borrows one projection per request. List, detail and batch requests share
typed monitor responses. `include` changes representation, not selection. Pagination,
full-list and batch limits bound response work. OpenAPI is generated from request and
response models. The site's `/api` page provides usage examples.

## Selection and presentation

`readmodel/query.py` stores sealed groups and pending conditions. Ordinary search uses
bare flags, such as `?Outdated&Yanked`. Different predicates use AND. Alternatives
within one build target or BuildSystem use OR. URLs join alternatives with `+`, for
example `?rva23_failed+rva23_succeeded&Yanked`. Advanced Search uses ordered
`TOKEN=AND|OR|NOT` and `Group=AND|OR|NOT` pairs. `advanced=1` selects an empty advanced
editor. The two modes cannot mix. Entering advanced mode preserves the ordinary
selection. Leaving advanced mode clears the expression but keeps the search text.
Preserve repeated keys in advanced queries. A group marker seals all pending conditions
since the preceding marker. Groups cannot nest or include an already sealed group. The
group operator is separate from its first condition's operator.

Evaluation starts from the scoped ALL and proceeds left to right. AND intersects, OR
unions and NOT subtracts the next operand. Pending conditions have no implicit
parentheses. Each sealed group also starts from the scoped ALL and evaluates left to
right. Its operator then combines its result with the outer result. Grouping preserves
every condition's operator.

`presentation/query_editor.py` adds or removes pending conditions and seals groups.
Removing a condition or group leaves other operators unchanged. `next_logic` controls
only the next action. Each sequence stores repeated conditions once. A sealed group and
pending conditions may share a condition. Four cyclic colors distinguish adjacent
groups, not severity or truth. `FilterGroups.astro` renders links from the server. The
browser does not evaluate queries. Navigation replaces controls, counts, packages and
pagination together and remains usable without JavaScript.

The list total counts packages that match the current expression. Ordinary counts show
intersections. For another alternative within a selected build target or BuildSystem,
the count shows new matches under all other conditions. Prefix/suffix intersections
compute those alternative scopes once per request, not once per candidate. Advanced
counts describe the next operation:

| Operator | Count |
|---|---|
| AND | Intersection of candidate and current packages |
| OR | Additions: candidate packages absent from the current result |
| NOT | Removals: current packages that match the candidate |

Each count is at most the candidate size. Changing the next operator never changes the
expression or current page. Clicking an already selected pending condition removes it,
independently of the next-addition operator. Indexed package sets implement union,
intersection and subtraction. They count each package once. Search bounds the scope.
Check counts target coverage. Other counts use the selected results or coverage view.
Pagination follows selection.

`MAX_QUERY_NODES` limits the total to 128 nodes. Conditions and explicit or implicit
union groups each consume nodes. Repeated conditions count before deduplication. No
per-group cap exists. Invalid operators, unknown dimensions, empty group operations and
over-budget inputs return 422. Responses publish the same limit. At the limit, users can
remove conditions but cannot add them. Dimension/value identifiers are limited to 100
characters.

`python scripts/benchmark-filters.py` measures 31 parse + selection + count samples on
6,000 synthetic packages with overlapping facts. Run it again before raising the budget.
Account for the deployment CPU and package count. Reverse proxies must accept query URLs
at the limit without truncation.

Presenters supply typed display primitives, captions, formatted values and evidence links.
CSS controls layout. The display catalog supplies palette identities.

Untracked means no configured upstream version track, excluding packages explicitly
marked not applicable. Other monitors do not change this classification. CheckFailed
counts packages, once each, with failed collection subchecks. Partial results,
watch-track and build-history failures retain their reasons in Checks. An OBS failed
build is a result, not a failed collection request. Unsupported, unconfigured and stale
checks keep their distinct meanings.

The aggregate page shows every matching dependency or build reason, grouping only
identical facts. Full provider fields and dependency conditions remain in detail. Issue
styles are declared once in `presentation/labels.toml`; BuildSystem identity styles live
in operator config.

Pages use locally bundled htmx to enhance native links and GET forms. One
rendered-result frame owns rows, counts and pagination. Foreground polls revalidate HTML
with ETag and compare document fingerprints before replacing the frame. Polling pauses
for hidden tabs, offline clients and active forms or menus. Failed or superseded
requests leave the previous frame intact. Each list/detail request fetches the current
display document. Unchanged documents reuse process-local rendered HTML, keyed by
document content, URL and cookies. Errors never reuse cached success. The LRU holds at
most 64 pages and 8 MiB of body/key/header bytes. It disappears on restart, so a release
cannot reuse an older renderer's output. ETag still validates the actual HTML. Response
scripts and dynamic evaluation are disabled. Changed application assets require a full
navigation.

## Observation identity and time

`monitors.version.compare.evaluate` makes the version decision. `evaluate_all` shares it
across a pass. Historical `last_known_relation` is evidence, not permission to run
upgrade checks. Upgrade-only adapters need a confirmed comparable newer release.
Current-version security checks do not.

The runner owns scheduling, persistence and retries. Adapters own inputs and
interpretation. Changed fingerprints queue work. Unchanged inputs wait for their refresh
interval. Failure retries are bounded, and HTTP cache age cannot exceed the effective
policy. A heartbeat does not write unchanged observations or settings. It can still
publish changes to input eligibility or catalog information.

A failed replacement retains one `last_result`, not a history chain. Only evidence for
the same query can reappear. Incompatible interpretations remain stale. A completed
result, including empty success, replaces the saved result. Combined release scopes
reuse each exact release independently. Revision-sensitive checks cannot use this reuse
path. The [adapter contract](monitor-porting.md#module-contract) defines scope.

| Field | Meaning |
|---|---|
| `attempted_at` | Last attempt, including failure |
| `checked_at` | Oldest HTTP input time; cached bytes retain their age. Without HTTP, execution time. |
| Scope `checked_at` | Last successful check for that scope; a combined result uses the latest successful scope time |
| `evidence_revision`, `changed_at` | Query-input or normalized-fact changes, not poll time/order |

Packaging-only revision changes need not invalidate upstream queries. EPSS changes
revise evidence, not advisory identity. Status error groups help users investigate
failures. Identical messages do not establish a common cause.

## Interpretation limits

| Observation | Does not establish |
|---|---|
| OSV identity/tag/commit or NVD CPE/version match | Local-patch, bundled-component or binary applicability |
| OSV `fixed` event | An openRuyi fix or recommended branch |
| CISA KEV / FIRST EPSS | Project urgency / complete risk score |
| Upstream EOL | Distribution support commitment |
| Same-project SPDX difference | Legal assessment or a comparison with RPM's aggregate License |

Security aliases are deduplicated. Failed enrichment preserves base advisories, not
invented KEV/EPSS values. Missing comparable license metadata is unsupported, not
unchanged. ABI comparison is absent. Reviewed CPE part/vendor/product mappings query
NVD's CVE API with the current source version. NVD matches version ranges. Git archive
tags come only from confined Source0 evidence consistent with RPM Version. Floating
branches and unresolved versions do not become release identities. Both providers use
the existing heartbeat and dated HTTP cache. NVD requests are paced at 6.5 seconds per
host. Failure retries back off from 15 minutes to one hour.

## Native and network boundaries

SPEC shell/Lua macros execute code. Each parse uses a new Linux worker, clean
environment, private result socket and scratch. Landlock permits installed runtime and
pinned inputs, not database/config/app files. The seccomp allow-list blocks network
sockets, process inspection/signals, namespace and mount operations. If confinement is
unavailable, parsing fails. There is no unrestricted fallback.

| Bound | Value |
|---|---|
| Concurrent workers / wall deadline | 4 / 5 s |
| Address space / CPU | 256 MiB / 3 s |
| FDs / file size | 32 / 2 MiB |
| Result / diagnostic output | 256 KiB / 16 KiB |
| Processes | 256, shared by real UID, not a private worker quota |

The parent kills the process group on timeout/output excess. Deployment bounds scratch
to 128 MiB. These limits do not eliminate denial of service or hide all filesystem
metadata. The kernel and runtime remain trusted dependencies. Enforcement is in
`monitors/source/rpm.py`, `spec_worker.py` and `spec_sandbox.py`. The worker pins RPM
`_tmppath` after loading macros. `TMPDIR` alone is insufficient. The worker sets target
context before parsing the SPEC. Macros can still redefine it. Expanded metadata is not
an OBS build or binary validation.

Adapter HTTP is confined to declared HTTPS hosts. Shared IO owns caching, pacing and
exclusive reusable connections. A transport failure closes only the affected connection.
Caller-injected clients remain caller-owned. The operator proxy applies only to monitor
HTTP. Browser CSP permits same-origin styles and list-page scripts, not inline or
external scripts. Other pages disable scripts. BuildSystem CSS is same-origin and
conditionally revalidated.

## Repository activity

GitHub collection owns `github_items` (repository/item ID), `github_links`
(package/evidence references) and per-repository checkpoints. It does not use
version-monitor invalidation. SQLite format 3 adds these row collections. Migration from
format 2 changes the storage header and preserves existing observations. The deployment
migrator backs up the database before conversion. Rollback to an older reader requires
the matching pre-migration backup.

Readers derive counts and cursor pages from one prepared snapshot. Details load 20
records per kind. Requesting more pauses background page replacement while the user
reads the expanded history. Navigation resumes normal refreshing. Provider bodies and
diffs are never rendered as HTML.
