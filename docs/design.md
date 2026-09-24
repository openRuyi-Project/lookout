# Maintainer reference

| Constraint | Reason | Implementation / check |
|---|---|---|
| HTTP readers never collect | Request latency and read-only authority | `api.py`, `view.py`, `scripts/check-architecture.py` |
| Each collector phase owns its fields | Concurrent phases cannot erase each other | `state.merge`, phase-ownership tests |
| Failed checks retain dated evidence | Failure is not absence of an update | `state.py`, streaming/targeted collector tests |
| Source identity precedes version inference | Similar names can identify different projects | `onboarding.propose`, native Source0 tests |
| Ordinary promotion preserves untouched text | A small rule correction needs a small review | `config_change.merge_text`, promotion tests |
| Generated API types follow OpenAPI | Avoid manual frontend schema synchronization | `scripts/api-types.py --check` |

Configuration syntax and operator commands live in [config/README.md](../config/README.md).
`version_rules.load` reads one native file once, returning entries, options and
its exact input digest. Discovery produces reviewable candidates, not runtime
rules. `package explain` locates the package's table; promotion compares the final
loaded rules, package policies and operator options with the reviewed result.

## OBS collection

Current status and successful-build history share a build identity, not a polling
cadence. The `builds` phase performs one project-wide `_result` request and owns
status/error/timestamps. The `obs` state phase owns source inventory and history;
`state.BUILD_FIELDS` restricts their writes within each build record. Publication
rebases onto the latest snapshot, so slow source/history work cannot rewind fast
statuses, and a status heartbeat cannot replace success provenance. Only complete
inventory removes packages; changing a target repository/architecture invalidates
that target's old observations. An in-flight status response for a changed scope
is discarded.

## Package selection

`build_status` owns OBS status meaning; `view` folds build flavors into one target
observation. `package_list` indexes those package rows once per request. List
membership and every count use intersections of the same sets, before pagination.
A facet ignores only its own selection when calculating available choices.
`build=TARGET:STATE` may repeat for distinct configured targets; selections across
targets are ANDed. Issues includes failed, unresolvable, broken and blocked, not
queued/running work, disabled targets or unknown observations. Staleness remains
independent of the observed status. The frontend renders API choices and preserves
selections in links. A small same-origin script submits the GET form immediately
on selection; it does not fetch, filter or count data. Without scripting, the form
retains a submit button.
Search and selections share one GET form. `listingQuery` normalizes its URL;
`PackageFilters` renders controls and `PackageTable` renders rows. Detail sections
reuse `EvidenceFacts` to format provider facts.

`base.css` owns theme pairs, native controls and focus defaults; `app.css` owns
shell and page layouts. Themes use CSS `light-dark()` (Baseline 2024). Wide tables
scroll horizontally; the document owns vertical scrolling.

## Native SPEC confinement

SPEC shell and Lua macros are executable input, not trusted metadata. Each parse
runs in a new Linux worker with a clean environment, no inherited application
files, a private result socket, and no network socket creation. Landlock permits
read access to the installed runtime and pinned input files; only the worker's
private scratch directory is writable. The collector database, operator config
and application source are not readable/writable by that worker. A seccomp
allow-list blocks process inspection, signals to other processes, namespaces and
mount changes. Missing confinement is a parse error, not a fallback to the parent.

The parent allows four workers and kills the whole worker process group after
five seconds or excess output. Worker limits include 256 MiB address space,
three CPU seconds, 32 file descriptors and 2 MiB **per file**. The 256-process
limit is shared with the real UID, not a private per-worker quota; the recommended
128 MiB `/tmp` mount bounds aggregate scratch storage. These are resource bounds,
not a claim that untrusted native code is free of all denial-of-service risk.
Landlock protects contents/access; it does not hide every filesystem metadata
fact. Runtime packages and the host kernel remain trusted dependencies.

RPM's `_tmppath` is explicitly set to private scratch after loading pinned macros;
`TMPDIR` alone does not constrain declarative `BuildSystem` parsing. The recorded
`native_query.context.target` and `rpm_target` are read from the actual initialized
RPM context before parsing, not a hard-coded RISC-V label or proof that a SPEC
cannot redefine its own macros. Source versions still come from the expanded RPM
header. This is metadata parsing, not an OBS target build or binary validation.


## Version decisions and monitor ports

`version_status.evaluate` owns source selection, freshness, compare policy and
upgrade eligibility. `view` and the monitor runner consume this same result;
`monitor_model.project` cannot accept a separately supplied upgrade boolean.
`evaluate_all` resolves each package once per pass, shared across adapters. The
historical `last_known_relation` remains evidence, not a second update decision.
A changed operator version policy waits for its snapshot publication before an
upgrade monitor runs; current-version monitors are independent of update status.

Configuration is static native TOML plus tracker policy. Loading/promoting it does
not read observations; discovery alone consumes saved Source0 to propose rules.
The retired automatic-rule identity guard and its unused snapshot plumbing are
not a fallback configuration engine.

The monitor runner owns lifecycle and storage; adapters own provider inputs and
interpretation; projection owns visibility. A new label uses the existing generic
API and renderer. Source, Version and Build use the same result envelope, with
typed domain payloads; v1 is a compatibility projection of v2 monitor results.
The UI renderer registry and layout are separate from the selector and linked
query facets. See the [monitor porting guide](monitor-porting.md) for the small
module contract, reusable components and an executable end-to-end example.

Upgrade monitors do not run without a confirmed newer comparable release.
LicenseChange compares same-project PyPI SPDX expressions, not free text against
RPM's aggregate License. Missing comparable metadata is unsupported, not equal.
ABIChange requires comparable old/new build artifacts and is not implemented.

### Security boundaries

OSV uses configured/derived ecosystem identity and returns **Security** evidence:
actual query identity/version, returned advisory IDs/aliases and matching-package
`fixed` events. These events do not establish an openRuyi fix or a recommended
upgrade branch. Local patch applicability is not evaluated. Aliases are
deduplicated. KEV is catalog membership, not an urgent priority. EPSS retains
its probability and model date, not a project risk score. Enrichment failures
retain base candidates without manufacturing KEV/EPSS values. This does not scan
bundled dependencies or binaries. EOL refers to upstream security support, not
the distribution's support commitment.

Traditional CPE identities may use the optional fixed `cve-bin-tool` component-list
adapter. Its isolated installation is read-only at `/opt/cve`, with its separately
managed database under `/data/cve/.cache/cve-bin-tool/`; web requests never update
or scan. Missing installation/database, an expired database (>2 days), and scanner
failures are explicit errors, **not zero CVEs**. Operators must provision and
refresh that database separately before counting this coverage. No source archive
extraction, source execution or arbitrary command is exposed by the adapter.
Dependency-Track's SBOM portfolio service is outside this lightweight tracker.

Only matching inputs may retain old findings after a provider failure; their
original observation time is not refreshed. Source changes or changed upgrade
targets invalidate them. Dashed Maintenance labels link to detail evidence and
coverage, rather than adding another global warning banner.

Provider network policy is separate from version/SPEC collection. An operator may
set `TRACKER_MONITOR_PROXY` (for example a locally managed HTTP proxy); only the
monitor HTTP client consumes it. Do not embed proxy addresses in package identity
data or apply a global proxy to OBS/Git merely to fix one provider. BuildSystem
colors are served as a small, conditionally revalidated same-origin stylesheet;
`style-src 'self'` is enforced. The list page permits same-origin scripts for
automatic filter submission; other pages retain `script-src 'none'`. Inline
scripts and external script origins are not enabled.


## Review state

Findings are observations, not human dispositions. This release does not add an
acknowledgement database, write API or issue service. A future read-only disposition
adapter must bind judgments to finding identity and relevant evidence revision;
poll timestamps alone must not invalidate a judgment. Source revision, local
patches, upstream identity/ranges and upgrade target are relevant changes.

`/api/v1/status` groups identical upstream provider errors with affected package
names and counts. These are triage groups, not a claim of a proven common outage;
per-package errors and old observations remain intact.

Finding-schema migration: old prose records remain stored but are not rendered
as structured facts. Checks reports `schema_changed` until recollection; adapter
VERSION changes invalidate cached inputs. Errors and stale structured evidence
remain visible. No synthetic conversion of prior advice into provider facts.

Monitor heartbeat (`heartbeat_seconds`, default 30) schedules bounded batches;
`interval_seconds` remains the minimum per-input recheck/retry interval. New
packages and changed inputs are automatically drained on successive heartbeats.
A no-work heartbeat does not publish a new snapshot. Failures use attempted time
for retry backoff and retain the last successful evidence. `checked_at` records a
successful check; `changed_at` and `evidence_revision` change only with subject or
evidence content, not response ordering or polling time. Daily EPSS values/dates
are evidence changes, but do not create a new advisory identity.
