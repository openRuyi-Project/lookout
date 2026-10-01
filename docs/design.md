# Design and trust boundaries

## Ownership

Installation references the image's version, identity and distribution catalogs;
`/config` contains operating settings and explicit overrides. A native override
replaces one complete track; a package override replaces one policy field or
complete monitor identity. All parsed files participate in publication and review
guards. Observation keys contain the effective rule/query, not its path or image
revision, so unrelated catalog changes preserve evidence. Migrating old copied
catalogs requires the original initialization baseline to distinguish defaults
from operator edits.

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

Executable adapters register in `monitors/registry.py`; readers consume the saved
catalog instead. Package initializers are inert. `scripts/check-architecture.py`
and Import Linter enforce import boundaries; `scripts/api-types.py --check` checks generated types
against OpenAPI. [Porting](monitor-porting.md) describes extension contracts;
[Configuration](../config/README.md) owns editing and promotion.

## Persistence and publication

Collectors fetch outside the writer lock, then merge onto the latest snapshot.
`state.merge` rejects fields outside a phase's ownership. A complete Git tree of
regular `SPECS/*/*.spec` files defines the package catalogue, including failed
parses and packages absent from OBS. Failed refreshes retain the confirmed catalogue.
OBS inventories describe build objects; they cannot add or remove source packages.
OBS status and history share an identity but not ownership:

| Phase | Writes |
|---|---|
| `builds` | Project-wide `_result` status, errors and poll timestamps |
| `obs` | Inventory, source identity and successful-build history |

`state.BUILD_FIELDS` prevents slow history responses from rewinding current
status. Changed target repository/architecture invalidates old evidence and
in-flight responses for that scope.

SQLite stores source, track, SPEC, build and monitor observations as keyed rows.
Transactions write changed rows, deletions and the snapshot header. Equal rows
retain their storage revision without JSON re-encoding. Incremental commits must
match the database revision and heartbeat they read; stale bases are rejected.
`state.read_cached` captures header, clock and row revisions in one transaction,
then decodes changed JSON outside the lock. Borrowed unchanged data is read-only.

A complete successful OBS poll with identical facts updates `snapshot_clock`, not
content generation or all build records. Partial/failed vectors cannot freshen
missing observations this way. Independent phase merges preserve the latest clock;
importing even equal content gets a new storage identity.

Storage `user_version` is independent of public snapshot `schema`. Writers require
the current format; the migration tool backs up, migrates transactionally and
compares observations. Unknown formats fail closed. Compatible image upgrades keep
the same data and query fingerprints; they do not reset evidence. Operational
commands and rollback limits belong in [Deployment](deployment.md#upgrade-and-recovery).

Writes use rollback journals and `synchronous=FULL`. API connections are read-only.
Startup recovery holds the writer lock and lets SQLite recover a hot journal;
it never deletes journals or replaces corrupt data. Standalone preflight does not
write an existing database.

### Prepared reads

A background task checks storage once per second. Changes, semantic freshness
deadlines or a backward clock jump rebuild the projection/index. A fresh clock-only
update can reuse evidence and update build timestamps alone. Publication atomically
swaps a complete model; concurrent readers keep the previous complete one.

Before initial publication, readiness fails. A failed or overdue refresh retains
the last model with a notice and degraded readiness. Liveness tests only the
Node → FastAPI chain. Neither endpoint proves provider coverage.

The fact API borrows one projection per request. List, detail and batch share typed
monitor responses; `include` changes representation, not selection. Pagination,
full-list and batch limits bound response work. OpenAPI is generated from the
request/response models; the site's `/api` page owns usage examples.

## Selection and presentation

`readmodel/query.py` owns the two-level query:

```json
{"groups":[
  {"logic":"and","conditions":[{"dimension":"build:rva23","value":"failed"},{"dimension":"build:rva23","value":"unresolvable","logic":"or"}]},
  {"logic":"and","conditions":[{"dimension":"maintenance","value":"Advisory"},{"dimension":"maintenance","value":"DepMismatch"}]}
]}
```

The URL/API `filters` parameter serializes this structure. No category replaces a
previous condition. Each link belongs to the following term. AND and NOT take precedence
over OR at each level; rows are parentheses. NOT subtracts its condition or group.
A leading NOT group complements that row within the scoped package universe;
leading AND/OR starts from the first nonempty row.
Empty groups are omitted from evaluation. Repeated conditions collapse within a group; groups can share conditions.
Indexed package sets implement the predicates and intersection/union/difference; unions count each
package once. Search and the chosen results/coverage view bound the dataset.

The list total and pagination use the current expression, independent of editing
mode. Candidate counts evaluate the whole expression after adding the candidate
idempotently to the active group using `next_logic`. In every mode, the displayed
count is `min(combined count, candidate's own count in the same scope)`, including
selected choices. This cap avoids repeating the union total on every option; it
is neither a new-package count nor an intersection, and never limits results.
Within a non-negated active row, AND candidate counts cannot exceed OR counts.
In a NOT row, OR expands the excluded set and can reduce the final result.
Check choices count coverage; other choices keep the current results/coverage
view. Search bounds both counts. Pagination follows selection, never precedes it.

`presentation/query_editor.py` performs immutable edits. The active group is a
positional editor ID outside `FilterQuery`; `next_logic` is also editor state.
Changing mode or selecting a row never rewrites existing predicates. The first
addition to an empty row sets its connector; subsequent additions keep that
connector. Removing the first condition keeps the row connector. Clearing a row
preserves the others; removing the final condition clears all empty rows and
resets the active row until the next condition.

One row uses ordered `AND-dimension=value` / `OR-dimension=value` / `NOT-dimension=value` parameters.
Repeated keys retain their positions, e.g. `AND-maintenance=A&OR-maintenance=B&AND-maintenance=C`
means `A OR (B AND C)`. Multiple rows use `filters` JSON, omitting default fields;
an empty editor row also needs JSON to retain its position. Mixing inline terms
with `filters` is rejected, as are repeated `filters` parameters. The HTTP adapter
reads ordered query pairs before Pydantic validation, which still owns all field
constraints. OpenAPI parameters are derived from those same model fields.
`filters=dimension=value` remains a valid single-condition input. All encodings
produce the same `FilterQuery` and normalized response. Links omit default request
parameters; editor state is included only when needed to continue an edit.
Adding a row inserts it immediately after the chosen row and selects it.
`presentation/navigation.py` projects edits as GET links and
`FilterGroups.astro` renders them. The browser has no second expression evaluator.
The existing HTML navigation/cancellation path updates groups, counts and rows
atomically; all edits also work without JavaScript.

`MAX_QUERY_NODES` is the single limit: 128 groups + conditions, counted before
deduplication. The fixed root is not a node. Pydantic rejects extra fields, nested
groups, unknown operators and over-budget input with 422; dimension/value names
retain a 100-character identifier bound. Reading documents and the fact API
publish the same limit, so the UI disables additions but leaves removal and
operator changes available. There is no per-group or group-count cap.

Budget measurement: `python scripts/benchmark-filters.py` builds 6,000 synthetic
packages with dense overlapping facts and 141 candidate counts. It measures 31
parse + selection + count samples for each query size and next-operator context.
The limit bounds indexed set operations, not network latency. Re-run the benchmark
before raising the bound; corpus size, deployment CPU and serialization also
matter. Reverse proxies must accept query URLs at the limit, not truncate them.

The website renders typed display primitives, not provider payloads. Presenters
own captions, value formatting and evidence links; CSS owns layout, with palette
identities supplied by the display catalog. OpenAPI generates the frontend types.

Untracked means no configured upstream version track, excluding packages explicitly
marked not applicable. Other monitors do not change this classification.
CheckFailed counts packages, once each, with failed collection subchecks. Partial
results, watch-track and build-history failures retain their reasons in Checks.
An OBS failed build is a result, not a failed collection request. Unsupported,
unconfigured and stale checks keep their distinct meanings.

The aggregate page shows every matching dependency or build reason, grouping only
identical facts. Full provider fields and dependency conditions remain in detail.
Issue styles are declared once in `presentation/labels.toml`; BuildSystem identity
styles live in operator config.

Read pages progressively enhance native links and GET forms with locally bundled
htmx. One rendered-result frame owns rows, counts and pagination. Foreground polls
revalidate HTML with ETag and compare document fingerprints before swapping; hidden
tabs, offline clients and active forms/menus pause polling. Failed or superseded
requests leave the previous frame intact. These checks reduce transfer and DOM
work, not server computation. Response scripts and dynamic evaluation are disabled;
changed application assets require a full navigation.

## Observation identity and time

`monitors.version.compare.evaluate` owns the version decision; `evaluate_all`
shares it across a pass. Historical `last_known_relation` is evidence, not permission
to run upgrade checks. Upgrade-only adapters need a confirmed comparable newer
release; current-version security checks do not.

The runner owns scheduling, persistence and retries. Adapters own inputs and
interpretation. Changed fingerprints queue work; unchanged inputs wait for their
refresh interval. Failure retries are bounded, and HTTP cache age cannot exceed
the effective policy. A heartbeat with unchanged observations/settings does not
write; it can still publish changed input eligibility or catalog information.

A failed replacement retains one `last_result`, not a history chain. Only matching
query evidence can reappear; incompatible interpretations are stale. A completed
result, including empty success, replaces the saved result. Combined release scopes
reuse each exact release independently; revision-sensitive checks cannot use that
shortcut. The [adapter contract](monitor-porting.md#module-contract) defines scope.

| Field | Meaning |
|---|---|
| `attempted_at` | Last attempt, including failure |
| `checked_at` | Oldest HTTP input time; cached bytes retain their age. Without HTTP, execution time. |
| Scope `checked_at` | Last successful check for that scope; a combined result uses the latest successful scope time |
| `evidence_revision`, `changed_at` | Query-input or normalized-fact changes, not poll time/order |

Packaging-only revision changes need not invalidate upstream queries. EPSS changes
revise evidence, not advisory identity. Status error groups aid triage; identical
messages do not establish a common cause.

## Interpretation limits

| Observation | Does not establish |
|---|---|
| OSV identity/tag/commit or NVD CPE/version match | Local-patch, bundled-component or binary applicability |
| OSV `fixed` event | An openRuyi fix or recommended branch |
| CISA KEV / FIRST EPSS | Project urgency / complete risk score |
| Upstream EOL | Distribution support commitment |
| Same-project SPDX difference | Legal assessment or a comparison with RPM's aggregate License |

Security aliases are deduplicated. Failed enrichment preserves base advisories,
not invented KEV/EPSS values. Missing comparable license metadata is unsupported,
not unchanged. ABI comparison is absent. Reviewed CPE part/vendor/product mappings
query NVD's CVE API with the current source version; NVD owns range matching.
Git archive tags come only from confined Source0 evidence consistent with RPM
Version. Floating branches and unresolved versions do not become release identities.
Both providers use the existing heartbeat and dated HTTP cache; NVD requests are
paced at 6.5 seconds per host and failures back off from 15 minutes to one hour.

## Native and network boundaries

SPEC shell/Lua macros execute code. Each parse uses a new Linux worker, clean
environment, private result socket and scratch. Landlock permits installed runtime
and pinned inputs, not database/config/app files. The seccomp allow-list blocks
network sockets, process inspection/signals, namespace and mount operations.
Missing confinement fails parsing; there is no unrestricted fallback.

| Bound | Value |
|---|---|
| Concurrent workers / wall deadline | 4 / 5 s |
| Address space / CPU | 256 MiB / 3 s |
| FDs / file size | 32 / 2 MiB |
| Result / diagnostic output | 256 KiB / 16 KiB |
| Processes | 256, shared by real UID, not a private worker quota |

The parent kills the process group on timeout/output excess. Deployment bounds
scratch to 128 MiB. These limits do not eliminate denial of service or hide all
filesystem metadata; kernel/runtime remain trusted. Enforcement is in
`monitors/source/rpm.py`, `spec_worker.py` and `spec_sandbox.py`.
RPM `_tmppath` is pinned after macro loading; `TMPDIR` alone is insufficient.
Recorded target context precedes SPEC parsing, not proof against macro redefinition.
Expanded metadata is not an OBS build or binary validation.

Adapter HTTP is confined to declared HTTPS hosts. Shared IO owns caching, pacing
and exclusive reusable connections; a failed transport retires only its connection.
Caller-injected clients remain caller-owned. The operator proxy applies only to
monitor HTTP. Browser CSP permits same-origin styles and list-page scripts, not
inline or external scripts; other pages disable scripts. BuildSystem CSS is
same-origin and conditionally revalidated.
