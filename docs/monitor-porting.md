# Port a monitor

Source, Version, Build and supplemental evidence share one **read contract**:
`{id, title, check, data}`. The fact API treats each as a monitor; pages consume a separate reading document. A monitor's
`check` describes collection, not the package: an OBS response containing `failed`
is a successful check with a failed build result. An unavailable check is never
converted to an empty successful result.

## Find the owner

Package-specific data is described in the [configuration guide](../config/README.md).
Implementation paths follow the fact being changed:

| Change | Entry point |
|---|---|
| Version selection or comparison | `backend/tracker/monitors/version/` |
| License comparison | `backend/tracker/monitors/license.py` |
| EOL query and release-cycle interpretation | `backend/tracker/monitors/eol.py` |
| PyPI / crates.io metadata shared by monitors | `backend/tracker/providers/pypi.py` / `cratesio.py` |
| Evidence collection registration | `backend/tracker/monitors/registry.py` |
| Facts → reading document | `backend/tracker/presentation/` |
| Document → HTML and layout | `frontend/src/components/document/` and `pages/` |

A short monitor can be a single module; split it into a package when it has
separate responsibilities, as Requires and Security do. Package initializers stay
side-effect free so importing a model does not import collection code. The
`nvchecker_source/` namespace is nvchecker's own discovery interface, not a second
monitor registry. Ordinary evidence ports reuse the shared presenter and renderer.

## Add a version source

An ordinary package needs only a native table in `config/versions/nvchecker.toml`.
If an existing nvchecker source cannot express a repeated provider protocol cleanly,
add a module under `backend/nvchecker_source/` with nvchecker's documented
`async get_version(name, conf, *, cache, **kwargs)` interface. No project registration,
config compiler, scheduler or frontend component is needed.

A source returns raw candidates; nvchecker owns common filtering, normalization and
comparison. `anitya_stable` deliberately returns only the provider's first stable
value, because provider ordering is authoritative. Use the supplied cache/session
so HTTP options and retries continue to work. Package identities and maintenance
lines belong in native TOML, not Python conditionals.

`crates_index` accepts `cratesio`, and `anitya_stable` accepts `anitya_id`;
`identity.request_url()` derives their standard provider address. An explicit
`url` replaces, rather than overrides, the identity field for custom endpoints.
Collection and public identity projection share this pure request boundary;
test the resulting URL and public identity as well as the selected version.
Go rules keep their explicit proxy URL. Release plugin rules with the matching
installed plugin/image, not as standalone built-in nvchecker rules.

Test the real CLI against local HTTP fixtures, malformed/empty responses, filter
ordering and shared-cache isolation. The installed-wheel test must find the plugin
beside nvchecker's built-in namespace. If the protocol identifies a registry used
by other monitors, extend the pure `identity.from_native` projection and verify it;
those monitors consume identity, not this source's implementation.

## Follow one result

```text
collector/adapter → owned snapshot fields → Monitor.read(Context)
                                               ↓
                               typed data + query dimensions
                                               ↓
                     /api/v2/packages (typed facts)
                                               ↓
                          presentation.registry.Presenter (pure read adapter)
                                               ↓
                        /api/ui/packages (reading document)
                                               ↓
                   generic Astro primitives → page layout
```

| Responsibility | Location | Contract |
|---|---|---|
| Read stored facts | `readmodel.monitors.Monitor` | Pure `project(Context)` returns `check`, typed `data`, and filter `dimensions`. |
| Prepare for reads | `readmodel.cache.ProjectionCache` | One background producer builds rows and the filter index, then publishes them together. HTTP reads never project the full snapshot. |
| Version decision | `monitors.version.compare` | One RPM-based decision, also consumed by upgrade monitors. |
| Filter/count | `readmodel.packages.PackageList` | Intersect monitor dimensions once; each facet excludes only its own selection. |
| HTTP schema | `api` | OpenAPI generates `api.generated.ts`; summary omits histories and full findings. Focused lists contain all compact identifiers and requirements for the requested package page, not provider fact bodies. |
| Reading adapters | `presentation.registry.PRESENTERS` | Source/version/build/evidence -> fields, tables, entries and links. No IO, HTML or CSS. |
| Display contract | `presentation.model` | Bounded, typed, non-recursive documents; OpenAPI generates TypeScript. No stored copy. |
| Website | `frontend/src/components/document/` | Values, fields, tables, navigation and facets. No monitor IDs or provider payload types. |
| Page composition | `frontend/src/pages/` | Responsive layout of documents; monitor selection is navigation separate from filters. |

The payload kinds are `source`, `version`, `build`, `requires`, and `evidence`. They retain
domain-specific structure rather than stringify build matrices or source history
into generic prose. Every monitor contributes to check-status filtering; existing
build, version and maintenance facets consume the same package rows.

The website opens **Results** for monitors with a standalone result presenter.
Evidence monitors list only packages with recorded findings; Version and Build
answer different reading questions with their own table shapes. Coverage rows
list collection status and last check time, including packages without a result.
An empty Results view is not a successful coverage claim. The shared check index
provides two clickable groups for every monitor: `uncovered` (`not_configured`,
`unsupported`) and `failed` (`error`). They count packages in the active search
and page-supported facet intersection, never findings or OBS build failures.
Pending, stale, partial, changed-input and inapplicable checks keep their precise
status in the detail's **Checks** section and exact-status links; these two groups
are not a complete coverage-rate denominator. There is no all-Checks homepage tab.
Both fact and document APIs default to Results; use `section=coverage` for
all checks, including packages without findings.

Monitor navigation is a reading policy, not a copy of the backend registry. A
`Presenter` always supplies detail sections; optional `columns` and `cells` give
it `has_results` and a main navigation entry. Source supplies package context, not
a second version table, so it has neither a Results table nor a default tab.
Existing UI `monitor=source` links remain valid and normalize to Source coverage,
including source check filters. The v2 catalog and Source data are unchanged.
Do not add a generic filler table merely to give every backend module a tab: a
standalone result view needs its own reading question.

A page's topic can combine related observations without combining collectors.
Version annotations reuse the validated current Security/Yanked evidence and
matching upgrade evidence; Requires contributes actual dependency changes, not
unchanged requirements or current unmet status alone. The owning monitor keeps
full facts and independent scheduling. Security still checks the current release
when it is already latest; provider matches do not establish local patch status.
Stable monitor IDs drive related filters, not display labels. The Version page
uses compact labels; Security exposes advisory IDs, Requires dependency rows.
Yanked is a Version annotation rather than a separate primary navigation item.
Detail labels point to the existing evidence sections rather than copy them.
Use the same category name in summaries, controls and findings, such as
`License` and `Security`. Distinct signals such as KEV remain separate.

Build system is one global sidebar selector, preserved across monitor changes and
search. Its counts use the current topic/search intersection, excluding only the
selected Build system. The always-visible rail and package badges share one
`buildsystem:<value>` appearance from operator configuration. Selected rail links
use that same color for their stroke and a pale background, not a second palette.
Topic-specific filters never leak to other pages. EOL is
last in the monitor navigation; collection Checks remain last on package details.

Linked facet choices come from the same package index. Both APIs default to Results when available.
The website forwards the query to the API and submits one GET form immediately
on selection. It does not reproduce filter validation, matching or counting.

An ordinary new evidence adapter needs **no frontend edit**: its title, findings,
source-attributed fields and check states flow through the existing evidence
presenter. Add an adapter, register/configure it, test collection and the resulting
read document. `test_new_monitor_uses_existing_document_primitives` exercises this
path with an unfamiliar monitor; the SSR test renders the actual Python projection.

A genuinely different factual shape (for example a new matrix) needs a typed read
payload and a pure `Presenter` in `presentation/`. Reuse fields/tables/entries;
only introduce a display primitive when these cannot express the information.
Do not give collectors a rendering hook, add a monitor-name switch in Astro, or
send HTML, component names, executable expressions or arbitrary styles. The
website owns spacing, breakpoints, themes, keyboard behavior and link safety.
Presentation tone is not a collector-assigned workflow priority.

Provider-specific formatting belongs to this reading boundary. Useful facts,
source links, dates and explicit unavailable states are visible without opening
individual disclosures. Common query facts appear once; an assertion repeated by
multiple records keeps all distinct source links. The UI distinguishes primary
observations from package context through layout, not collapsed evidence. Raw
responses remain available through the fact API; a missing result is not negative.
A same-name version track is an implementation detail, not a second package fact.

Snapshot ownership and batching remain separate from presentation: the OBS
collector still issues bulk requests, the SPEC collector uses its isolated worker,
and provider adapters use the bounded monitor runner. A shared result contract
does **not** require calling OBS once per package or a base class with collection
hooks that some monitors cannot implement.

## Add an evidence monitor

Use [License adapter](../backend/tracker/monitors/license.py) for an upgrade-only
port, or [EOL adapter](../backend/tracker/monitors/eol.py) for a current-version
port. Both are ordinary modules implementing `monitors.contract.Adapter`; no inheritance,
plugin loader or separate service is needed.

| Member | Contract |
|---|---|
| `TITLE` | Optional display title; defaults to the registry ID. Published with observations, never inferred from a finding label. |
| `VERSION` | Positive interpretation version. Change it when the same inputs would mean different facts; not for every code edit. |
| `HOSTS` | Set of exact provider HTTPS hostnames. Scoped IO rejects other hosts, credentials, redirects and nonstandard ports. |
| `SCOPE` | Optional `current` (default), `upgrade`, or `current_and_upgrade` (Requires). Upgrade-only jobs require the saved version decision used by the UI to be `outdated`; Requires always checks the current release and optionally its confirmed upgrade. |
| `inputs(package, configured)` | Pure function; return a JSON-compatible dict, or `None` when no reliable identity exists. No network, filesystem or subprocess work. |
| `query_subject(subject, inputs)` | Optional pure dependency projection for `check()`. Defaults to the entire subject; omit only fields that cannot affect the result. |
| `refresh(subject, inputs, previous)` | Optional pure function returning `monitors.schedule.Schedule`; default is a six-hour recheck. It may use prior facts and current inputs, but performs no IO. |
| `check(subject, inputs, io)` | Fetch/interpret provider observations; return exactly `status`, `findings`, `note`. Status is `ok`, `partial` or `unsupported`. Raise on failed/invalid provider responses. |

`package` contains `name`, RPM-expanded `version`, source `revision` and the
reviewed native rule's public `identity`. Upgrade context also has `target_version`.
`configured` is only this monitor's `[NAME.monitors.ID]` value from `config/packages.toml`.
`subject` has the same source fields but no native rule; `inputs` is the resolved
provider-specific identity. An explicit identity takes precedence over derivation.
Use `identity.from_native` for supported registry protocols; do not infer
upstream identity from an RPM package prefix or a shared homepage.

Use `io.json(method, url, body=None)` and `io.today`. IO owns HTTP limits, cache,
request deduplication and the operator proxy. A monitor does not create its own
HTTP client, load global configuration, read the snapshot or write state. The
optional fixed cve-bin-tool adapter is a separately constrained subprocess, not
a general executable path available in configuration.

A provider may pass `min_interval=1.0` to space network requests to its host across
workers and monitors. Cache hits do not consume that budget. HTTP 429/503 pauses
further requests to that host for the current batch (at least 60 seconds, or the
longer `Retry-After`); other hosts continue. The normal retry schedule remains in
charge of later batches. This is not a cross-process quota: run one collector.

## Facts and failure

Construct results with `monitors.model.finding` and `monitors.model.evidence`.
A finding ID identifies the same assertion across polls; it must not contain a
poll timestamp. Labels/tags are single-word or CamelCase categories, not priority
or action assignments. Evidence carries its source and HTTPS link.

`key` is display text. Use an optional stable `code` when a machine consumer needs
to recognize a field. The evidence presenter consumes `query` and `epss_probability`; changing their
display text must not change selection or formatting.
Other codes remain ordinary visible evidence, without frontend registration.
Old structured records without codes remain readable; no English-label inference
is used to manufacture codes for them.

Missing evidence is not `false`: use `unavailable`, `not_applicable` or
`not_evaluated` with a null value. `ok` plus an empty findings list means a completed
check found no matching condition. For partial enrichment, retain the base facts
and mark the missing fields explicitly. Neither case asserts that a package is
generally safe, supported or fixed.

The runner owns input fingerprints, execution of refresh policies, batching, provider fairness,
snapshot publication and exact-input failure retention. An `inputs()` exception
is local to that package/monitor; a `check()` exception retains only matching old
evidence without refreshing its successful-check time. Changed query inputs or
upgrade targets invalidate old evidence. The default dependency set includes source
revision; adapters that query only upstream versions can explicitly narrow it. An
idle heartbeat writes nothing.

A temporary input-availability gate is separate from the provider result. The
runner retains the result, retry counter and timestamps; coverage shows
`input_unavailable` and retained findings are marked old. Restoring identical
inputs reuses a fresh result, while an expired result or changed query is checked
again. An uncertain upgrade comparison cannot start a new upgrade check, but does
not erase already-recorded evidence for the same version pair.

## Refresh policy

Modules choose **when evidence needs another check**; the runner controls concurrency,
locks, retries and publication. No base class, extra timer or configuration-loaded
Python is needed:

```python
from tracker.monitors.schedule import Schedule


def refresh(subject, inputs, previous):
    return Schedule(interval_seconds=21600, retry_seconds=300, max_retry_seconds=3600)
```

This function can choose different intervals from its inputs or previous result.
The runner calls it on each heartbeat. A changed source/identity/adapter/upgrade
fingerprint is due regardless of the normal interval; an unchanged fingerprint
is due after its interval. `error` and `partial` use exponential retry delays from
`attempted_at`, capped by `max_retry_seconds`. Success resets the counter. Batch
limits and the heartbeat mean these are earliest eligibility times, not deadlines.
The transport cache cannot outlive the effective recheck/retry age; shared requests
are still deduplicated. The existing cache's six-hour upper limit also remains.

`query_subject()` separates query dependencies from source provenance. Security and
License use `monitors.model.version_query`: only the current/target version enters
the subject portion of the fingerprint. EOL uses the release cycle. Resolved
`inputs` and adapter `VERSION` are always included. A packaging-only revision can
therefore rebind matching upstream evidence to current source context without a
provider call or a new `checked_at`/`changed_at`. This does not evaluate local patches.
An adapter that inspects patches must keep source revision in its dependencies.
Publication recomputes these dependencies and rechecks source availability, so
an in-flight response cannot attach to a different query or hide a failed source.

Defaults: Security and EOL recheck every six hours; License rechecks the version
pair every twelve hours and still requires a confirmed upgrade. These checks must
not run **only** on version changes: new vulnerabilities, lifecycle dates or metadata
corrections can affect an unchanged version. An unchanged result advances
`checked_at`, not its `changed_at` or `evidence_revision`.

Operator overrides belong to `tracker.toml`, separately from package identities:

```toml
[monitors.refresh.security]
interval_seconds = 3600
retry_seconds = 300
max_retry_seconds = 3600
```

Each monitor uses its module policy unless a per-monitor override is configured. `stale_after_seconds` must
exceed the effective normal interval. `monitor explain` reports effective timing
and whether the input is due without running a check.

OBS, Git and nvchecker remain batch collectors, not one adapter invocation per
package. Their modules expose `polling(config)`/`polling(spec)` using the same
`Schedule`; the supervisor only runs and waits. Operational intervals remain in
`[collector]` and `[spec]`. OBS status is a single project-wide request, Git skips
unchanged history. Git traverses commits since its saved ancestor and retries
failed packages; a missing ancestor, macro change or parser-policy change performs
full reconciliation. New OBS inventory members get their own historical changelogs.
The upstream heartbeat uses `--due` to select changed rules, expired observations
or retryable failures. A source-only RPM change needs a new comparison, not a new
provider query. Each track still expires periodically; no manual heartbeat is needed.
A normal collector invocation without `--due` remains an explicit full check. No poll is started by an HTTP page request.

## Executable example and acceptance

[The Yanked adapter](../backend/tracker/monitors/yanked.py) reads the current
release's `info.yanked` assertion from the [PyPI JSON API](https://docs.pypi.org/api/json/).
It does not infer release withdrawal from a subset of files. A missing assertion
is unsupported, not false. `providers.release` adapts PyPI and crates.io release
facts for License and Yanked. Requires backends reuse the same provider transport.
Each registry normalizes its own fields into `providers.model.Release`; a new registry
registers in `providers/release.py`, without a provider-name branch in License or Yanked.
One crates.io project response serves current/target versions, multiple packaged
release lines, and all three monitors; no extra HTTP client is needed. Crate
identity comes from reviewed nvchecker entries, never the RPM name.

[test_monitor_porting.py](../backend/tests/monitors/test_monitor_porting.py) registers it
through the existing registry and runs the actual heartbeat, scoped IO, SQLite
storage, monitor projection, catalog, check-status and Maintenance filters. Provider HTTP is the substituted
boundary. The tests cover findings, empty results, failed requests, source changes,
invalid inputs and isolation from other packages. No scheduler/API/UI branch is
added for the new label.

For a real port:

1. Add `backend/tracker/monitors/ID.py` and register the trusted module once in
   `monitors.registry.REGISTRY` under a stable ID (`source`, `version`, and `build` are reserved).
   Add your provider fixtures and tests alongside the example.
2. Enable `ID` in `[monitors].enabled`. Add per-package identity exceptions only
   where `inputs()` cannot derive a reviewed identity. Never put scripts or
   credentials in these identities.
3. Run the existing [development and native checks](../CONTRIBUTING.md). Include
   wrong/missing identity, malformed responses, no finding, timeout, unchanged
   evidence, due-time boundaries, cache expiry and changed inputs. Upgrade ports also test no upgrade, disabled
   comparison, stale upstream and changed target through the shared runner.
4. The next heartbeat publishes the catalog and results. The selector, labels,
   evidence detail and check-status counts work without edits to API routes or
   page components. `frontend/tests/render.mjs` exercises an unregistered renderer
   and a renamed Security finding label to protect this boundary. If a different
   reading form is needed, change the pure presenter using existing document
   primitives. Do not add domain-specific branches to the website.
5. Inspect one real package with the read-only commands below, then promote the
   configuration and release through the [deployment procedure](deployment.md).
   Observe check statuses/errors and evidence timestamps, not only label counts.

```sh
PYTHONPATH=backend python -m tracker.monitors explain PACKAGE --monitor ID \
  --config config/tracker.toml --db /path/to/snapshot-copy.sqlite3
PYTHONPATH=backend python -m tracker.monitors check PACKAGE --monitor ID \
  --config config/tracker.toml --db /path/to/snapshot-copy.sqlite3
```

`explain` is offline. `check` may contact declared providers but does not write the
snapshot or a production cache. The current contract is for source-version facts;
binary ABI, repository-only health or human dispositions need their own explicit
inputs before they can be truthfully attached. Do not fetch hidden prerequisites
inside the renderer or bypass the source gate to make a port appear covered.

## Add a dependency-constraint backend

**Requires is a domain, not a Python monitor.** Its page presents upstream runtime
requirements, including non-Python libraries. Providers own declarations for exact
upstream releases; comparators interpret version syntax; Source supplies only the
current local dependency version used for assessment. A SPEC dependency list is
not substituted for upstream declarations. Build-toolchain facts retain their
`build` kind and are not presented as runtime requirements.

1. Implement `inputs(package, configured)` and `read(version, settings, io)` in
   a trusted backend module. Return `Requirement` values: stable dependency ID,
   display name, build/runtime kind, comparison scheme, original declaration,
   normalized comparison value, source and URL, plus reviewed ecosystem identity,
   conditions and extras when present. Register it in
   `monitors.requires.monitor.BACKENDS`; do not add a new top-level monitor for each language.
2. Reuse a comparator in `monitors.requires.compare.COMPARATORS`. If the syntax is new,
   add and test its isolated comparator. `pep440` uses packaging;
   `rpm_version` uses native RPM VERSION ordering and rejects Epoch/Release and
   capability expressions it cannot establish. Unsupported syntax stays unknown.
3. The read projection builds an identity index from reviewed native rules and
   resolves a dependency only when one source package matches. Missing or ambiguous
   matches remain unknown. Use `[openruyi.dependencies]` for reviewed exceptions,
   not a mandatory per-package allowlist. Do not infer identities from RPM names
   or put package-name mappings in Python or Astro.
4. Test current-only, upgrade-only, changed/unchanged declarations, missing metadata,
   real comparison semantics, stale dependency observations, ambiguous identities,
   conditions/extras, and a non-Python runtime through the same API and presenter.
   Shared comparison code must not branch on dependency names.

`monitors.requires.pypi` reads exact-release `Requires-Python` and `Requires-Dist` declarations.
Environment markers and extras stay attached to their clauses; they are not
evaluated on the collector host, and conditional assessments remain unknown.
Unsupported declarations and absent metadata are not an empty dependency set.
`monitors.requires.cratesio` reads Cargo's
`rust-version`, a minimum build-toolchain version. `numeric_minimum` compares bare
numeric releases; RPM suffixes and prereleases remain unknown rather than being
silently stripped. The API preserves the original bare declaration and comparison
scheme; numeric runtime minimums can be formatted as ≥ without claiming Cargo's
compiler is a runtime dependency. These backends do not claim full dependency
coverage, installed artifact compatibility, RPM capability resolution or a solver graph.

Requires uses `SCOPE = 'current_and_upgrade'`: the current release is independently
observable without an available upgrade. An optional confirmed target is checked
separately. Each release keeps its own check status and successful-check timestamp;
a failed target check must not make a successful current observation disappear.
Unchanged observations refresh check time while retaining `changed_at` and a stable
`evidence_revision`. A dependency version change re-assesses the saved declaration
without polling its provider or rewriting its evidence.

The `requires` fact API exposes optional current/target constraints, their sources,
the resolved package, comparison scheme and dated local source observation.
`satisfaction` assesses the current declaration; `target_satisfaction` assesses the
target independently. A missing side is unknown, not proof of an added or removed
dependency. Only two observed, fresh, unequal declarations establish `changed`.

The website shows **Requires**, dependency, current requirement with ✓/✗/?, and
current local source version. A changed pair appends → target requirement with its
own mark. Current-only rows do not invent a version pair; upgrade-only rows say
**Upgrade:** explicitly. These marks establish only a version-constraint result,
not build compatibility or installed artifacts. Generic document primitives render
the result; no provider- or Requires-specific Astro/JavaScript logic is needed.

Adapters classify optional feature dependencies through `optional`; this is not
an evaluation of platform applicability. Unclassified conditions retain
`None`. The list separates Runtime and Optional, while complete condition
expressions remain in the detail's closed Dependency conditions section and raw
API. Equal assessments under different conditions share one summary; identity,
constraints, source URLs, extras and observations must still match.

`mapping` is independent of satisfaction. Missing or ambiguous identity mappings
are not proof of an unpackaged dependency. **Not packaged** requires an explicit
mapping whose target is absent from a fresh, successful inventory; unavailable
inventory or source versions remain unknown. No missing mapping creates an Unmet
result by itself.

The linked **All / Unmet / Changes / Uncovered / Failed** navigation uses
`PackageList` intersections and stays the same in result and coverage views.
`requires=unmet` means at least one explicitly unsatisfied **current runtime**
requirement; target-only failures and unknown current assessments do not qualify.
`requires=changes` selects observed changed runtime pairs. Packages count once,
even with several matching requirements. Website choices share the search and
Build system context; choosing coverage clears the requirement filter so an
unobserved package is not required to already have an unmet observation. The v2
API publishes `requires_counts` and supports cross-monitor filter combinations.

## Measure coverage

Keep four counts separate: packages with reviewed identity, currently eligible
checks, successful checks (including zero findings), and findings. Upgrade-only
License does not run without a confirmed upgrade; Requires still checks the
current release and keeps current/upgrade coverage separate. Unknown input,
missing provider fields and network failures are separate from no finding. A new
backend must improve these counts on the same snapshot without changing version
rules or treating a missing assertion as false. EOL uses explicit product/cycle
data in `config/packages.toml`; its applicable population is not the whole registry.

## Change a layout or introduce a new payload kind

For a different homepage arrangement, change the document components/page layout.
For a different selection of facts, change the pure presenter. Keep filtering and
counting in `PackageList`; the website does not normalize or interpret dimensions.
The default overview keeps Version and per-target Build columns, BuildSystem beside
the name and evidence labels below it. A monitor with result columns shows them
in Results; a context-only monitor has Coverage instead. Controls belong to the
reading form: Overview offers Build and Maintenance filters, Build results offer
three single-choice target rows (combined across targets), Version offers
All/Updates and inline Related choices, and Requires offers All/Unmet/Changes.
Overview keeps target dropdowns. Build selectors expose OBS status values, not synthetic aggregate statuses.
Coverage offers only search, Build system, and exact check-status deep links.
All focused monitors expose Uncovered and Failed without a redundant all-Checks
tab. These count missing identity/metadata and check errors, respectively, not
zero findings or build failures.

Out of date links select `freshness=retained`: packages with visible old evidence,
not every failed check or hidden watch observation. Membership is projected after
Version composition and indexed alongside other dimensions. Counts and row links
use that same membership; a failed check without prior evidence is not retained.

Switching monitor or mode preserves only filters the destination can display;
the UI API applies the same rule to directly entered URLs before selection.
Search, Build system and page size remain shared. The v2 fact API still supports
cross-monitor filter combinations for external consumers. Do not implement a
second query policy in Astro or hide active filters in opaque links.
Visible selectors expose their selected value and an All choice; do not duplicate
these as removable chips. Exact check-status deep links retain an indicator when
there is no matching selector. Search submits the full GET form even without
JavaScript; JavaScript only adds immediate select submission.

The renderer uses column roles, not monitor IDs, to keep build matrices scrollable
while simple tables reflow on narrow screens. A single pagination component uses
the server-provided links at both ends of multipage results; one-page results
need only the final count. No frontend filter arithmetic or router state is added.

Detail documents contain primary sections and supporting context. Both remain
visible: build results and evidence in the main column, package information in the
side column, with checks and changelog below. Source still supplies package
metadata, the SPEC link, build-system identity and changelog, and keeps its own
dated Checks row even though it is absent from the default rail. Normal version/track identifiers
already expressed by the heading do not create another section. Do not turn an
absence of duplication into extra disclosure clicks.

Build's focused list places brief OBS reasons under their target status, with
links to complete details. Flavor-specific reasons keep their flavor identity;
the overview remains compact. Status matrices use the same generic table
renderer, which allocates readable columns from column roles, not monitor IDs.

Use the existing `evidence` payload for sourced assertions. A genuinely different
shape (like Build's target/flavor matrix) requires a typed payload in `api`, its
pure projection in `readmodel.monitors`, and a reading adapter using the document
primitives. Regenerate the UI types only if the reading contract changes. This is
an intentional schema boundary, not a reason to introduce untyped JSON or a
runtime plugin loader. Read projections grant no collection privileges.
