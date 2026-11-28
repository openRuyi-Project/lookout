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
| Version discovery | `monitors/version/` | Proposes candidates. Only reviewed native rules execute |
| External protocols | `monitors/`, `providers/` | Attributed facts, not maintainer decisions |
| Writes | `collector.py`, `state.py`, `storage.py` | Phase-owned fields, one serialized transaction |
| Derived facts and selection | `readmodel/` | Saved input only, without collection |
| Reading order and grouping | `presentation/` | Pure fields, tables, entries and links |
| Layout and interaction | `frontend/` | Renders documents and submits queries, without monitor inference |

Adapters register in `monitors/registry.py`. Readers use the saved catalog. Package
initializers do not run collection or registration. `scripts/check-architecture.py` and
Import Linter enforce import boundaries. `scripts/api-types.py --check` checks generated
types against OpenAPI. [Porting](monitor-porting.md) describes extension contracts.

## Persistence and publication

### Write ownership

Collectors fetch outside the writer lock. Under the lock, they merge onto the
latest snapshot. `state.merge` rejects fields outside the phase's ownership.

The confirmed Git tree of regular `SPECS/*/*.spec` files owns the package catalog.
It includes failed parses and packages absent from OBS. OBS supplies build objects,
not source-package membership. These phases own separate fields, even when they
share a record:

| Phase | Writes |
|---|---|
| `builds` | Project-wide `_result` status, errors and poll timestamps |
| `obs` | Inventory, source identity and successful-build history |
| `github` | `github_items`, `github_links` and repository checkpoints, independent of version-monitor invalidation |

`state.BUILD_FIELDS` prevents slow history responses from overwriting newer status.
A target repository or architecture change invalidates that scope's evidence and
pending responses.

### SQLite transactions

SQLite stores observations as keyed rows. Each transaction writes changed rows,
deletions and the snapshot header. Equal rows retain their revision. Storage
compares payload digests with database rows, not mutable caller objects.

Every commit to an existing database must match the base storage revision and
heartbeat. Heartbeat writes use the same compare-and-swap check. A commit without
a base can only initialize a database. Imports get a new storage identity even
when content is unchanged.

OBS coverage uses the inventory and targets at commit time. A package added during
a poll stays unknown until observed. Missing results keep their last successful
timestamps. Partial coverage keeps the normal poll interval. No usable status triggers backoff. Only a complete, unchanged poll updates `snapshot_clock`
instead of the build rows. Other phases preserve that clock.

`state.read_cached` reads the header, clock and row revisions in one transaction.
It decodes changed JSON after the transaction. Reused nested data is read-only.
API connections cannot write.

### Failure and recovery

A failed refresh retains the confirmed catalog. A failed SPEC parse retains the
last successful metadata, revision and observation time, with a new error and
attempt time. Invalid shared macros reject the whole batch.

SPEC invalidation compares Git trees, including merge resolutions. Reads use a
pinned commit. Changelogs are display history, not cache keys. Cached metadata
requires matching SPEC bytes, macro and auxiliary inputs, and resolver version.
Installed RPM versions, system macros and parser code identify the runtime.
A changed selector or runtime identity requires a full SPEC pass.

After a Git fetch failure, Lookout can parse the confirmed local revision again.
This does not advance the remote observation time or clear the fetch error.
Source failures degrade readiness but do not fail HTTP liveness.

Writes use rollback journals and `synchronous=FULL`. Startup recovery holds the
writer lock while SQLite recovers a hot journal. It never removes journals or
replaces corrupt data. Standalone preflight does not write an existing database.

Storage `user_version` and public snapshot `schema` are separate. Writers require
the current storage format. Migration backs up the database, changes it in one
transaction and compares observations. Unknown formats are rejected. Compatible
image upgrades retain data and evidence. See [Deployment](deployment.md#image-upgrades)
for upgrade and rollback procedures.

### Prepared reads

A background task checks storage once per second. It rebuilds the projection and
index when data changes, a freshness deadline expires or the clock moves backward.
Fresh clock-only updates reuse unchanged facets and monitor search text. Changed
build timestamps remain searchable.

The task prepares status aggregates once per publication. It replaces the model
atomically. Existing requests keep their model. Refresh-failure notices remain live.

Readiness fails until the background task publishes the first model. If a refresh fails or is overdue,
Lookout retains the last model, shows a notice and reports degraded readiness. Liveness
tests only the Node → FastAPI chain. Neither endpoint proves provider coverage.

The fact API borrows one projection per request. List, detail and batch requests share
typed monitor responses. `include` changes representation, not selection. Pagination,
full-list and batch limits bound response work. Request and response models generate OpenAPI. The site's `/api` page provides usage examples.

## Selection and presentation

### Queries

`readmodel/query.py` evaluates indexed package sets within the search scope.
TestsDisabled identifies a packaging action that removes riscv64 check commands. For one plain `%check` header and a known provider, RPM parses a second
copy with `%check -a` on that line. If this restores commands to an absent,
empty or comment-only stage, the detail records the original line. A single literal opt-in test switch can
also establish the cause if enabling it restores commands for the same package,
version and BuildSystem. Neither an
empty default nor a difference from x86_64 proves that packaging disabled tests.
This compares expanded scripts, not test execution or maintainer intent.
For macros shipped outside the SPEC tree, `monitors/source/macrosets/<package>/<git-tree>/`
holds the reviewed provider files and their upstream revision. A changed provider
tree cannot reuse those files. Other SPEC metadata does not use these overlays. In the package detail API,
`source.data.check_provider` identifies the file that supplies the default,
its digest and its source. It does not claim that the SPEC used the default.

Finding filters count distinct matching package IDs before pagination. Two advisories
on one package count as one package.

An empty query returns all packages. Each selected value is a positive predicate:
values within a facet form a union. Different facets intersect. Removing the last
value removes that facet. Counts apply the search text and every other facet.
Prefix/suffix intersections compute these scopes once per request.

Finding filters can include retained findings from a failed refresh.
Data status shows failed, unsupported, pending and stale checks.

Selected filters remain visible and removable even when their counts reach zero.
Menu selections also appear above the table. The website's `/api` page defines the
URL format.

`MAX_QUERY_NODES` limits a request to 128 selections, counting raw repeats.
Dimension/value identifiers cannot exceed 100 characters. Invalid parameters,
unknown dimensions and excess selections return 422.
Run `python scripts/benchmark-filters.py` before raising the limit.

### Display contracts

Presenters supply typed display primitives and links. Astro renders them without evaluating monitor facts or queries.
Issue styles come from `presentation/labels.toml`. BuildSystem styles come from operator config.

| Classification | Meaning |
|---|---|
| Untracked | No upstream version rule |
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
The browser disables response scripts and dynamic evaluation.
Each activity response reads one snapshot and defaults to 20 records per kind.
Cursors bind subsequent pages to that history revision.
Loading more pauses replacement until navigation resumes. Provider bodies and diffs are never rendered as HTML.

## Observation identity and time

`monitors.version.compare.evaluate` makes the version decision. `evaluate_all` shares it
across a pass. Historical `last_known_relation` is evidence, not permission to run
upgrade checks. Upgrade-only adapters need a confirmed comparable newer release.
Current-version security checks do not.

The adapter defines which inputs identify its query. A changed input fingerprint
queues work. Unrelated catalog changes do not. The refresh policy bounds retries
and HTTP cache age.
A heartbeat writes only changed observations, eligibility, settings or catalog data.

On failure, only dated evidence for the same query can reappear. The runner keeps
one `last_result`. An older interpretation stays stale. A valid,
complete query with no matches replaces earlier findings. If the primary query
succeeds but enrichment fails, its new findings remain visible. Combined checks
apply retention [per release scope](monitor-porting.md#module-contract).

| Field | Meaning |
|---|---|
| `subject` | Package and release under observation |
| `inputs` | Provider identity and settings for the query |
| `query_fingerprint` | Hash of provider, projected query subject and resolved inputs. Excludes the adapter implementation version |
| `fingerprint` | Input fingerprint. Includes the query and adapter implementation version |
| `attempted_at` | Last attempt, including failure |
| `checked_at` | Oldest HTTP input time, or execution time without HTTP. Cached bytes retain their age. |
| Scope `checked_at` | Last successful check for that scope. A combined result uses the latest successful scope time |
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

Security merges advisory aliases. Missing comparable license metadata is unsupported, not
unchanged. ABI comparison is absent. Reviewed CPE part/vendor/product mappings query
NVD's CVE API with the current source version. NVD matches version ranges. Git archive
tags come only from confined Source0 evidence consistent with RPM Version. Floating
branches and unresolved versions do not become release identities. Lookout spaces NVD requests 6.5 seconds apart per host. Failure retries back off from 15 minutes to one hour.

## GitHub collection

GitHub activity has its own identity and checkpoint. Package version changes do
not invalidate issues or pull requests. SQLite retains both open and closed items.

### Polls and checkpoints

Incremental polls use `updated_at`, a two-minute overlap and ETags. They reuse
unchanged records and PR paths. Weekly reconciliation detects missing records and
rechecks open PR revisions. Each list page processes issues before PRs. Matching
uses changed paths first, then bounded package-name searches in text.

A batch starts at most `request_budget` requests within 120 seconds. Requests run
serially, at least one second apart. Anonymous batches start at most every ten
minutes and use at most 60 requests. Quota headers can stop a batch sooner.
The client reserves 50 authenticated or two anonymous requests for other consumers.

A completed anonymous poll with no changes adds one interval to the next delay.
Changes reset it. Errors and unfinished batches do not count as empty polls.
Authenticated polls use the configured fixed interval. The freshness deadline
grows with the idle delay.

The next due time, list page and PR file-page progress survive restarts. File lists
are published only after checking that the base and head revisions still match.
GitHub returns at most 3,000 files per PR. Incomplete lists remain explicit, not
empty matches.

### Authentication and backoff

The activity collector and host updater retain authentication cooldowns across
restarts. HTTP 401 or an explicit token permission failure retries the public
endpoint once without credentials, then pauses token use for one hour.

The next heartbeat after that hour probes authentication. The probe preserves the
anonymous delay, quota wait and cursor. Only a successful authenticated request
resumes authenticated work immediately. Rate limits and network failures do not
trigger anonymous retries. Provider cooldowns take precedence over the schedule.

### Activity reads

`/api/v2/packages/NAME/activity?kind=pr` (or `issue`) returns records newest-updated
first. The default page has 20 records, with `per_page` capped at 50. `next_cursor`
identifies the next page and binds the package, record kind and history revision.
Treat it as opaque. Changed history returns HTTP 409: restart without the cursor.
Unrelated collection heartbeats do not invalidate cursors. Invalid cursors return 422.

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

Adapters can send HTTP requests only to declared HTTPS hosts. Shared IO owns caching, pacing and
exclusive reusable connections. A transport failure closes only the affected connection.
Caller-injected clients remain caller-owned. The operator proxy applies only to monitor
HTTP. Browser CSP permits same-origin styles and list-page scripts, not inline or
external scripts. Other pages disable scripts. BuildSystem CSS is same-origin and
conditionally revalidated.

### Query and export boundaries

Target IDs must be nonempty strings. Their `build:` facet names allow at most
100 characters and cannot contain `=`. Facet values can contain `=`.

JSON export streams packages in index order from one prepared snapshot. It does
not construct a second full response model in memory. A serialization or transport
failure terminates the download. Consumers must reject incomplete JSON. Export is
not a database backup.

List `total` and `counts.all` report the [selected package count](#queries).
Other entries in `counts` exclude the current view selection.
