# Design and trust boundaries

## Ownership

| Responsibility | Owner | Boundary |
|---|---|---|
| Operator settings and package policy | `config.py` | Loads explicit files; does not inspect observations |
| Native version rules | `monitors/version/` | Discovery proposes candidates; only reviewed configuration runs |
| Provider collection | `monitors/`, `providers/` | Produces attributed observations, not maintainer dispositions |
| Snapshot publication | `state.py`, `collector.py` | Serializes writers and enforces phase-owned fields |
| Read models and filtering | `readmodel/` | Uses saved observations; never invokes collectors |
| Reading documents | `presentation/` | Selects and formats facts for a view |
| Layout and interaction | `frontend/` | Renders documents and submits selections; does not infer monitor results |

[Configuration](../config/README.md) describes editing and promotion.
[Monitor porting](monitor-porting.md) describes adapter contracts and registration.
[CONTRIBUTING](../CONTRIBUTING.md) lists development checks.

Executable adapters are registered in `monitors/registry.py`. Readers consume the
saved catalog, not the executable registry. Package initializers are inert; pure
models and comparisons can be shared without importing collection. The recursive
import check in `scripts/check-architecture.py` enforces this boundary. Generated
frontend API types are checked against OpenAPI by `scripts/api-types.py --check`.

## Snapshot consistency

Collector phases fetch outside the snapshot writer lock, then merge their results
onto the latest snapshot. `state.merge` rejects fields outside each phase's
ownership. Only a complete source inventory removes packages.

OBS status and successful-build history share a build identity, but have separate
cadences and owners. The `builds` phase makes a project-wide `_result` request and
owns status, errors and their timestamps. The `obs` phase owns source inventory
and build history. `state.BUILD_FIELDS` limits writes within a build record: slow
history work cannot rewind current status, and a status poll cannot overwrite
success provenance. Changing a target repository or architecture invalidates its
old observations; responses collected for the old scope are discarded.

### Content revision and observation clock

A complete, successful OBS status poll with unchanged facts updates only SQLite's
`snapshot_clock` row. Payload and content generation remain unchanged. Partial,
failed or changed vectors take the full-snapshot path; missing observations cannot
become fresh through a clock update.

`state.read_cached` reads the payload and clock in one SQLite transaction. Each
full commit folds in the clock and assigns a new storage revision, even when an
import retains the content generation. Cache reuse requires the same storage
revision and database file identity. SQLite backups include both tables; readers
require the current clock schema.

Writes use rollback journals and `synchronous=FULL`. API connections are read-only.
On startup, under the writer lock, SQLite may recover an interrupted transaction;
the application does not delete journals, replace corrupt data or create a fake
successful snapshot. Standalone preflight is read-only against an existing database.

### Read-model publication

One background task checks the database once per second. It prepares a complete
projection and filter index when the database changes, a freshness deadline is
reached, or the wall clock moves backwards. If no freshness boundary is crossed, a clock-only update refreshes build
timestamps without recomputing unrelated evidence. Publication swaps the prepared
model under a lock; requests can keep reading the previous complete model during
preparation.

Before the first model exists, readiness fails. A failed refresh or overdue
freshness calculation retains the previous model with a notice and degraded
readiness. Liveness checks only the web-to-API HTTP chain, not collection health.

## Selection and presentation

`monitors.build.status` defines OBS status meaning; `readmodel.monitors` folds
build flavors into target observations. `readmodel.packages` indexes each prepared
projection. Package membership and facet counts use intersections of the same
sets before pagination. A facet excludes its own selection when calculating its
choices. Build selections are ANDed across targets; observed status and staleness
remain independent.

The UI API validates selections and returns controls, tables and fields. Astro
renders them without provider-specific interpretation. Search and selections
share a GET form; a same-origin script submits changed selections immediately.
Without JavaScript the submit button remains available.

## Evidence and freshness

`monitors.version.compare.evaluate` supplies one version decision to readers and
collectors. `evaluate_all` shares it across adapters for a pass. Historical
`last_known_relation` is evidence, not permission to run an upgrade check. Upgrade
checks require a confirmed newer, comparable release and published version policy;
current-version checks do not depend on an available upgrade.

The runner owns scheduling and storage; adapters own provider inputs and factual
interpretation. Each adapter supplies a pure refresh policy. Input changes queue
work immediately; unchanged inputs receive periodic checks. Failed or partial
checks use persisted retry counts. Work runs in bounded batches and HTTP cache age is capped by the effective
policy. A heartbeat with no provider jobs may still publish input invalidations
or catalog changes; unchanged stored observations and settings require no write.
Collection schedules and operator overrides are defined in `monitors/schedule.py`
and each collector's `polling()` or adapter's `refresh()`.

Old findings survive a provider failure only while their query inputs still
match. Their original successful-check time is retained. Query changes, changed
upgrade targets or an adapter `VERSION` change invalidate reuse. Rebinding source
context alone does not refresh external evidence. Invalid saved findings produce
`schema_changed`, not invented replacement facts.

Timestamp and revision fields have distinct meanings:

| Field | Meaning |
|---|---|
| `attempted_at` | Last attempted check, including failure |
| `checked_at` | For a single-scope adapter, last accepted result, including partial/unsupported; a thrown failure retains the prior time |
| Scope `checked_at` | Last successful result for that scope; the combined observation uses the latest successful scope time |
| `evidence_revision` / `changed_at` | Revision and time of query-input or normalized finding changes, not polling time or result order |

A packaging-only source rebind need not change the query fingerprint or evidence
revision. An EPSS value/date update changes evidence, but not advisory identity. `/api/v2/status` groups identical provider errors for triage; the groups
do not establish a common cause, and package-level evidence remains available.

### Interpretation limits

| Observation | What it does not establish |
|---|---|
| OSV query result for an ecosystem identity/version | Applicability to openRuyi patches, bundled dependencies or binary artifacts |
| OSV matching-package `fixed` events | An openRuyi fix or a recommended upgrade branch |
| CISA KEV membership | A project-assigned urgency |
| FIRST EPSS probability and model date | A complete risk score |
| Upstream EOL | The distribution's support commitment |
| Changed same-project SPDX metadata | A legal assessment or a comparison with RPM's aggregate License |

Security aliases are deduplicated. Enrichment failures preserve base advisory
results without manufacturing KEV/EPSS values. Missing comparable license metadata
is unsupported, not unchanged. ABI comparison is not implemented.

The optional CPE component-list adapter runs a fixed, read-only `cve-bin-tool`
installation under `/opt/cve`, using `$TRACKER_CVE_HOME/.cache/cve-bin-tool/` (default home: `/data/cve`). Missing or
failed scanner/database checks, including a database older than two days, are
errors rather than zero CVEs. Operators provision the scanner and database
separately. The adapter does not extract source archives or expose arbitrary
commands. Web requests never scan or update that database.

## Native SPEC confinement

SPEC shell and Lua macros execute code. Each parse runs in a new Linux worker with
a clean environment, no inherited application files, and a private result socket.
It cannot create network sockets. Landlock permits reads of the installed runtime
and pinned inputs; only private scratch is writable. The collector database,
operator configuration and application source are outside its allowed paths.
A seccomp allow-list blocks process inspection, signals to other processes,
namespace and mount changes. Missing confinement fails the parse.

The parent permits four concurrent workers and kills the whole process group on
timeout or excess output. Bounds are:

| Resource | Bound |
|---|---|
| Parent wall-clock deadline | 5 seconds |
| Worker address space / CPU | 256 MiB / 3 seconds |
| File descriptors / individual file size | 32 / 2 MiB |
| Result / diagnostic output | 256 KiB / 16 KiB |
| Processes | 256, shared by the real UID, not a private worker quota |

The deployment template bounds aggregate scratch storage with a 128 MiB `/tmp`.
These limits do not eliminate every denial-of-service risk. Landlock restricts
access but does not hide all filesystem metadata; the runtime and kernel remain
trusted. See `monitors/source/rpm.py` and `spec_sandbox.py` for enforcement.

RPM's `_tmppath` is set to private scratch after loading pinned macros; `TMPDIR`
alone does not constrain declarative BuildSystem parsing. Recorded native target
fields come from the initialized RPM context before parsing. They do not prove a
SPEC cannot redefine macros. Versions come from the expanded RPM header: this is
metadata parsing, not an OBS target build or binary validation.

## Network and browser boundaries

Monitor HTTP is scoped to each adapter's declared HTTPS hosts. The optional
`TRACKER_MONITOR_PROXY` is consumed only by that client, not OBS or Git; it belongs
in operator settings, not package identity data.

BuildSystem colors are served through a conditionally revalidated same-origin
stylesheet. CSP requires `style-src 'self'`; the list page permits same-origin
scripts for form submission, while other pages use `script-src 'none'`. Inline
scripts and external script origins are not enabled.
