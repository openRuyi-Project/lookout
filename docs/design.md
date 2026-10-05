# Design and trust boundaries

## Ownership

Image catalogs supply default rules and identities. `/config` supplies operator overrides.
Publication and review checks include every parsed input.
See [Configuration](../config/README.md) for override rules.

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
GitHub collection separately owns `github_items`, `github_links` and repository checkpoints.
Its records do not use version-monitor invalidation.
Transactions write changed rows, deletions and the snapshot header. Equal rows retain
their storage revision without JSON re-encoding. Incremental commits are rejected unless their base database revision and heartbeat still match. `state.read_cached` captures header, clock and row revisions in one transaction,
then decodes changed JSON outside the lock. Borrowed unchanged data is read-only.

A complete successful OBS poll with identical facts updates `snapshot_clock`, not
content generation or all build records. Partial or failed polls cannot update
timestamps for missing observations this way. Independent phase merges preserve the
latest clock. Imports get a new storage identity, even when their content is unchanged.

Storage `user_version` is independent of public snapshot `schema`. Writers require the
current format. The migration tool backs up the database, migrates it in a transaction
and compares observations. It rejects unknown formats. Compatible image upgrades retain data and evidence. Operational commands and rollback limits are in
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
Refresh-failure notices remain live. Publication atomically replaces the model while existing readers retain the previous model.

Readiness fails until the first model is published. If a refresh fails or is overdue,
Lookout retains the last model, shows a notice and reports degraded readiness. Liveness
tests only the Node → FastAPI chain. Neither endpoint proves provider coverage.

The fact API borrows one projection per request. List, detail and batch requests share
typed monitor responses. `include` changes representation, not selection. Pagination,
full-list and batch limits bound response work. OpenAPI is generated from request and
response models. The site's `/api` page provides usage examples.

## Selection and presentation

### Queries

`readmodel/query.py` evaluates indexed package sets within the search scope.
Each package counts once. Pagination follows selection.

| Mode | Combination |
|---|---|
| Ordinary | AND between predicates. OR between alternatives for one build target or BuildSystem. |
| Advanced | Evaluate conditions left to right from scoped ALL. AND intersects, OR unions, NOT subtracts. |

A group marker seals pending conditions since the preceding marker. Groups cannot nest.
Each group evaluates from scoped ALL, then combines with the outer result using its own operator.
Grouping preserves condition operators. The group operator is independent of the first condition.
Repeated conditions collapse within a sequence, not across groups.

`presentation/query_editor.py` changes the query, not its evaluation rules.
Selection and candidate counts use the same indexed sets.
See the site's `/api` page for URL syntax and candidate-count semantics.
Prefix/suffix intersections compute alternative scopes once per request.

`MAX_QUERY_NODES` limits queries to 128 conditions and group nodes, including implicit union groups.
Repeated conditions count before deduplication. There is no per-group limit.
Dimension/value identifiers are limited to 100 characters.
Invalid operators, unknown dimensions, empty group operations and excess nodes return 422.
Responses expose the limit. At the limit, removal remains available but additions are disabled.

Run `python scripts/benchmark-filters.py` before raising the limit.
It measures parsing, selection and counting on synthetic packages.
Account for deployment CPU, package count and reverse-proxy URL limits.

### Display contracts

Presenters supply typed display primitives and links. Astro renders them without evaluating monitor facts or queries.
Issue styles come from `presentation/labels.toml`; BuildSystem styles come from operator config.

| Classification | Meaning |
|---|---|
| Untracked | No configured upstream version track, excluding packages marked not applicable |
| CheckFailed | At least one failed collection subcheck, counted once per package |

Checks retain reasons for partial results, watch-track failures and build-history failures.
An OBS failed build is a build result, not a collection failure.
Unsupported, unconfigured and stale checks remain distinct.

The aggregate page groups identical facts but retains every matching dependency and build reason.
Detail pages retain provider fields and dependency conditions.

### Browser updates

Locally bundled htmx enhances native links and GET forms, which remain usable without JavaScript.
Navigation replaces controls, counts, rows and pagination together.
Polling pauses for hidden tabs, offline clients and active forms or menus.
Failed or superseded requests retain the current frame.

ETag validates rendered HTML. Document fingerprints prevent replacement of unchanged content.
The process-local HTML cache uses document content, URL and cookies as its key.
It stores at most 64 pages and 8 MiB of body/key/header bytes. Errors cannot reuse cached success.
Restarting clears the cache. Changed application assets require full navigation.
Response scripts and dynamic evaluation are disabled.
Activity pages read counts and cursor pages from one snapshot, initially showing 20 records per kind.
Loading more pauses replacement until navigation resumes. Provider bodies and diffs are never rendered as HTML.

## Observation identity and time

`monitors.version.compare.evaluate` makes the version decision. `evaluate_all` shares it
across a pass. Historical `last_known_relation` is evidence, not permission to run
upgrade checks. Upgrade-only adapters need a confirmed comparable newer release.
Current-version security checks do not.

The runner schedules and stores checks. Adapters define inputs and interpretation.
Query identity excludes file paths and image revisions, so unrelated catalog changes preserve evidence. Changed fingerprints queue work. Unchanged inputs wait for their refresh
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
revise evidence, not advisory identity. Identical error messages do not establish a common cause.

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
branches and unresolved versions do not become release identities. NVD requests are paced at 6.5 seconds per host. Failure retries back off from 15 minutes to one hour.

## Native and network boundaries

SPEC shell/Lua macros execute code. Each parse uses a new Linux worker, clean
environment, private result socket and scratch. Landlock permits installed runtime and
pinned inputs, not database/config/app files. The seccomp allow-list blocks network
sockets, process inspection/signals, namespace and mount operations. Parsing fails if confinement is unavailable.

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
