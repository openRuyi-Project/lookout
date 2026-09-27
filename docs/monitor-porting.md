# Add a monitor

Choose the smallest extension that supplies the missing observation:

| Need | Change |
|---|---|
| Track another package's version | Add a native rule; see the [configuration guide](../config/README.md). |
| Read a version-provider protocol nvchecker does not support | Add an [nvchecker source](#add-a-version-source). |
| Obtain another assertion about a source release | Add an [evidence adapter](#add-an-evidence-adapter). |
| Read dependency declarations from another ecosystem | Add a [Requires backend](#add-a-requires-backend). |
| Read license or withdrawal metadata from another registry | Add a backend in `backend/tracker/providers/release.py`. |
| Present existing facts differently | Change the [reading adapter or layout](#change-presentation), not collection. |

Package identities and policy belong in configuration. Provider protocols belong
in code. See [Contributing](../CONTRIBUTING.md) for the directory map and checks,
and [Design](design.md) for snapshot ownership and security boundaries.

## Add an evidence adapter

Start with [EOL](../backend/tracker/monitors/eol.py) for a current-release check,
[License](../backend/tracker/monitors/license.py) for an upgrade-only check, or
[Yanked](../backend/tracker/monitors/yanked.py) for a small registry-backed example.

1. Add `backend/tracker/monitors/ID.py`, implementing the
   [Adapter contract](../backend/tracker/monitors/contract.py). Use a package only
   when the implementation has distinct responsibilities to separate.
2. Register the trusted module in `monitors.registry.REGISTRY`. The IDs `source`,
   `version` and `build` are reserved. Add `ID` to `[monitors].enabled` in the
   operator configuration.
3. Resolve the identity in `inputs()`. Reuse `identity.from_package()` for saved
   Source0 or native-rule identities; explicit monitor configuration takes
   precedence. Put exceptions in `[NAME.monitors.ID]` in `config/packages.toml`,
   not package-name branches in Python.
4. Fetch through the supplied `io`, and return source-attributed facts. Reuse
   `providers/` when another monitor already reads that protocol.
5. Test the adapter through the runner and read projection, then inspect a package
   with the contributor CLI. Release code and configuration together using the
   [deployment procedure](deployment.md).

An ordinary evidence adapter needs no API route or frontend component. The saved
catalog, evidence presenter and generic renderer supply its title, findings,
coverage and detail sections. A new *kind of data*, such as a matrix, has a
separate [presentation path](#change-presentation).

### Module contract

| Member | Meaning |
|---|---|
| `VERSION` | Interpretation revision included in the query fingerprint. Increment when unchanged inputs would produce differently interpreted facts. |
| `HOSTS` | Exact HTTPS hosts permitted by scoped IO. Credentials, redirects, fragments and nonstandard ports are rejected. |
| `TITLE` | Optional display title; defaults to the registry ID. |
| `SCOPE` | `current` by default, or `upgrade` / `current_and_upgrade`. |
| `inputs(package, configured)` | Pure function returning a JSON-compatible dict, or `None` if a reliable identity cannot be established. |
| `check(subject, inputs, io)` | Fetch and interpret observations; return `status`, `findings` and `note`. |
| `query_subject(subject, inputs)` | Optional pure projection used **only for fingerprinting**. `check()` still receives the full subject. |
| `refresh(subject, inputs, previous)` | Optional pure function returning a `Schedule`; the default recheck interval is six hours. |

`package` supplies the source name and revision, the configured native rule's
public `identity`, and saved `source_release` evidence when available. Its version
is the upstream release identified by Source0 when established, otherwise the
current source version. `configured` is this monitor's value, not the entire
configuration. `subject` contains the same name/version/revision and, for upgrade
scopes, `target_version`. The runner passes neither a snapshot nor a storage
handle to an adapter.

For `current` and `upgrade`, `check()` returns `status` equal to `ok`, `partial`
or `unsupported`. Raise on failed or malformed provider responses. The runner
isolates exceptions to that package/monitor and retains only matching old evidence.
`upgrade` runs only for a confirmed upgrade from the shared version comparison;
it must not implement a second comparison.

`current_and_upgrade` additionally returns `scope_checks`: one `current` entry,
and an `upgrade` entry when a target exists. Each entry contains `status`
(`ok`, `error` or `unsupported`) and `note`; the overall status may also be `error`.
Findings name their own scope and upgrade target. See
[Requires](../backend/tracker/monitors/requires/monitor.py): a failed target check
must not discard a successful current-release observation.

This combined scope is limited to exact upstream-release checks.
`runner.same_scope()` matches adapter version, resolved inputs and release
versions, not source revision or the full query fingerprint. It cannot safely
reuse facts dependent on local patches or other revision-specific input; use a
single scope for those checks.

### Facts and failure

Build results with `monitors.model.finding()` and `evidence()`. Their Pydantic
models validate the ingestion contract; use the models rather than a parallel
schema in the adapter.

- A finding ID identifies an assertion across polls. Do not include poll time.
- Labels and tags classify facts; they do not assign urgency or maintainer actions.
- Each evidence field carries its source and HTTPS URL. `key` is display text;
  supply a stable `code` if a machine consumer must recognize the field.
- Missing evidence has a null value with `unavailable`, `not_applicable` or
  `not_evaluated`. It is not `false`.
- `ok` with no findings means the check completed without a matching condition,
  not that the package is generally safe. `partial` retains usable facts and
  marks missing enrichment explicitly.

A check describes collection, not the observed package. A successful OBS query
can report a failed build. Likewise, zero findings and an unconfigured check are
different results.

### IO and refresh

Use `io.json(method, url, body=None)` and `io.today`. Shared IO owns bounded HTTP,
cache, request deduplication and the operator proxy. Adapters neither create HTTP
clients nor read/write snapshots. `min_interval=1.0` can space requests to a host
across workers sharing that IO; cache hits do not consume the interval. HTTP
429/503 defers further requests to that host using `Retry-After` with a 60-second
minimum. This is a single-collector budget, not a cross-process quota.

Choose timing in the module when provider semantics require it:

```python
from tracker.monitors.schedule import Schedule


def refresh(subject, inputs, previous):
    return Schedule(interval_seconds=21600, retry_seconds=300, max_retry_seconds=3600)
```

The runner evaluates the policy on each heartbeat. Changed fingerprints are due
immediately; unchanged queries wait for their interval. Errors and partial
results use capped exponential retry delays. These are earliest eligible times,
not deadlines: batch limits and worker availability still apply. Transport cache
age is bounded by the effective recheck/retry policy. Operator overrides live in
`[monitors.refresh.ID]`; the stale threshold must exceed the normal interval.

The fingerprint always includes the adapter ID, `VERSION` and resolved `inputs`.
Its subject portion defaults to the whole source context. Narrow it only when
omitted fields cannot change the provider query or its interpretation:

- `monitors.model.version_query` retains the current/target version pair for
  upstream metadata checks.
- EOL retains the derived release cycle.
- A single-scope check that examines local patches must retain the relevant source
  revision.

A packaging-only revision can then reuse upstream evidence without a provider
call. Publication rechecks source availability and the fingerprint; combined
checks may retain an independently matching release scope. A successful unchanged
poll advances `checked_at`, not `changed_at` or `evidence_revision`. A heartbeat
skips publication when its projected state is unchanged; input invalidation or
catalog changes can still require a write
without provider jobs. A temporary source failure gates retained evidence without
turning it into a fresh result.
Periodic rechecks remain necessary: vulnerabilities and provider corrections can
arrive without a version change.

OBS, Git and nvchecker remain batch collectors with their own `polling()` policy.
The common read contract does not require per-package OBS calls or a collection
base class. HTTP page requests do not initiate polling.

### Verify the port

Use [test_monitor_porting.py](../backend/tests/monitors/test_monitor_porting.py)
as the integration example. It substitutes provider HTTP while exercising the
runner, SQLite publication, catalog, projection and facets.

| Case | Required observation |
|---|---|
| Valid response, with and without a finding | Facts or a completed empty result; both distinguishable from missing coverage. |
| Missing identity or unsupported metadata | Explicit unconfigured/unsupported state, not an empty success. |
| Invalid inputs or timeout | Local failure; other packages still progress. |
| Same query, unchanged facts | Successful-check time advances; evidence revision remains stable. |
| Changed query or in-flight source change | Old evidence cannot become a fresh result for a different subject. |
| Due-time boundary and cached response | Retry/refresh policy remains effective through transport caching. |
| Upgrade-only check | No check without a confirmed upgrade; stale or changed targets cannot attach evidence. |
| Reading document | Source links and unknown states survive; no monitor-name branch is needed in Astro. |

Inspect configuration offline, then check against a snapshot copy:

```sh
PYTHONPATH=backend python -m tracker.monitors explain PACKAGE --monitor ID \
  --config config/tracker.toml --format human
PYTHONPATH=backend python -m tracker.monitors check PACKAGE --monitor ID \
  --config config/tracker.toml --db /path/to/snapshot-copy.sqlite3
```

`explain` also accepts `--db` to show saved source context and due status. `check`
may contact declared providers but writes neither the snapshot nor a disk cache.
Follow the [development checks](../CONTRIBUTING.md) before release. Measure identity
coverage, eligible checks, successful checks and findings separately on the same
snapshot. Findings alone are not a coverage metric.

## Add a Requires backend

Requires collects upstream declarations for exact releases. Local Source versions
are used only to assess those declarations; SPEC dependencies are not a substitute
for upstream evidence.

1. Implement `inputs(package, configured)` and `read(version, settings, io)` in
   `backend/tracker/monitors/requires/`. Register the module in `monitor.BACKENDS`.
2. Return `Requirement` values carrying a stable dependency ID, `runtime` or
   `build` kind, comparison scheme, declaration, source URL, and identity,
   conditions and extras where supplied. Keep provider parsing separate from the
   read-side assessment.
3. Reuse a comparator in `requires.compare.COMPARATORS`, or add an isolated one
   with tests for supported and rejected syntax. Comparators must not branch on
   dependency names.
4. Test both release scopes, missing metadata, stale observations, ambiguous
   mappings and conditions through the existing API and presenter.

The [PyPI backend](../backend/tracker/monitors/requires/pypi.py) reads
`Requires-Python` and `Requires-Dist`. Markers/extras are preserved, not evaluated
against the collector host. `optional` classifies feature selection, not platform
applicability. Conditional assessments therefore stay unknown. The
[crates.io backend](../backend/tracker/monitors/requires/cratesio.py) records
`rust-version` as a **build** requirement, not a runtime dependency.

The read-side resolver uses reviewed native identities and
`[openruyi.dependencies]` exceptions. An absent or ambiguous mapping stays
unknown. `not_packaged` requires an explicit mapping absent from a fresh,
successful inventory. Satisfaction compares a declared constraint with an
observed source version; it does not establish installability or binary
compatibility.

Current and target requirements are assessed independently. A missing side is
not proof of addition/removal. Only two fresh, observed, unequal declarations
establish a change. A local dependency version change can reassess saved
requirements without querying the upstream provider again. The runtime list and
Unmet filter exclude build requirements; the raw findings retain their kind.

## Add a version source

Use native nvchecker sources and options first. When several rules need a missing
provider protocol, add `backend/nvchecker_source/NAME.py` implementing nvchecker's
`async get_version(name, conf, *, cache, **kwargs)` interface. Reuse its cache and
HTTP session; no project monitor registration or frontend work is required.

The source supplies candidates; nvchecker applies common filters, normalization
and comparison. Provider selection semantics still matter: `anitya_stable`
returns the provider's first stable value, not an unordered history for
a different comparator. Package identities and maintenance lines stay in TOML.

`identity.request_url()` is shared by native collection and identity projection.
For `crates_index` use `cratesio`; for `anitya_stable` use `anitya_id`. A custom
`url` replaces the identity field; supplying both is rejected. Go proxy sources
use an explicit URL. If another monitor needs an identity from the new protocol,
extend and test `identity.from_native()` rather than importing the source plugin.

Test the actual nvchecker CLI against local HTTP fixtures, including malformed
and empty responses, filtering order and cache isolation. The wheel test must
find the plugin beside nvchecker's built-in namespace. Release plugin rules with
the corresponding installed code, not as standalone built-in nvchecker rules.

## Change presentation

The collection and reading paths meet only at saved facts:

```text
collector → snapshot → readmodel.monitors → /api/v2/packages
                              ↓
                    presentation.Presenter → /api/ui/packages
                                                     ↓
                                           Astro document renderer
```

| Change | Owner |
|---|---|
| Stored facts → typed payload and filter dimensions | `backend/tracker/readmodel/monitors.py` |
| Filter intersections and facet counts | `backend/tracker/readmodel/packages.py` |
| Facts → fields, tables, entries and links | `backend/tracker/presentation/` |
| Spacing, responsive layout, themes and keyboard behavior | `frontend/src/components/document/` and `pages/` |
| Public schema and generated TypeScript | `backend/tracker/api.py` and `frontend/src/lib/api.generated.ts` |

Evidence adapters already use the `evidence` payload and presenter. A different
shape needs a typed read payload, its projection and a pure `Presenter` using
existing document primitives. Add a rendering primitive only if fields, tables
and entries cannot express the information. Collectors do not supply HTML,
component names or executable presentation rules.

A presenter always supplies detail sections. A standalone Results page additionally
needs both `columns` and `cells`; a context-only monitor need not have a second
list. Source is such a context, rather than another Version table. Choose which
facts a reading view combines in the presenter, without merging collectors.

Filter choices and counts come from `PackageList`; the website submits the GET
query rather than repeating the arithmetic. The UI API keeps only filters the
selected page can display. The fact API permits cross-monitor combinations.
Coverage includes packages without findings; Uncovered and Failed select specific
check states, not every non-success state or OBS build failure. Keep collection
status distinguishable from the observed fact in both APIs.
