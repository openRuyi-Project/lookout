# Port a monitor

Source, Version, Build and supplemental evidence share one **read contract**:
`{id, title, check, data}`. The API and pages treat each as a monitor. A monitor's
`check` describes collection, not the package: an OBS response containing `failed`
is a successful check with a failed build result. An unavailable check is never
converted to an empty successful result.

## Follow one result

```text
collector/adapter → owned snapshot fields → Monitor.read(Context)
                                               ↓
                               typed data + query dimensions
                                               ↓
                     /api/v2/packages → renderer → page layout
```

| Responsibility | Location | Contract |
|---|---|---|
| Read stored facts | `monitor_views.Monitor` | Pure `project(Context)` returns `check`, typed `data`, and filter `dimensions`. |
| Version decision | `version_status` | One RPM-based decision, also consumed by upgrade monitors. |
| Filter/count | `package_list.PackageList` | Intersect monitor dimensions once; each facet excludes only its own selection. |
| HTTP schema | `api` | OpenAPI generates `api.generated.ts`; summary omits histories and full findings. Focused evidence lists include a bounded preview and total finding count. |
| Presentation | `frontend/src/monitors/registry.ts` | Summary/detail components by payload kind; optional specialization by stable monitor ID. |
| Page composition | `frontend/src/monitors/layout.ts` | Place components without collecting, filtering or counting. |
| Monitor selection | `MonitorSelector.astro` | Consume the API catalog; selecting a monitor changes focus, not collection configuration. |

The payload kinds are `source`, `version`, `build`, and `evidence`. They retain
domain-specific structure rather than stringify build matrices or source history
into generic prose. Every monitor contributes to check-status filtering; existing
build, version and maintenance facets consume the same package rows.

The website opens a monitor's **Results** view. Evidence monitors list only
packages with recorded findings; source, version and build retain their own table
shape. **Coverage** lists collection status and last check time, including packages
without a result. An empty Results view is not a successful coverage claim.
Both views use the same query dimensions and global filter context; check-status
counts describe coverage, not just packages with findings. Direct v2 API requests
default to coverage for compatibility; use `section=results` explicitly.

Monitor navigation comes from the catalog. Contextual controls and summary/detail
components belong to the renderer, not to adapter-produced HTML or a UI schema.
License reuses the Version column beside its change preview; Security shows the
advisory count with evidence on the package page. Other evidence ports get the
bounded title preview automatically. A renderer's `context` lists existing monitor
IDs it needs as companion columns; it does not recompute their facts.

`/api/v1/packages` is a compatibility projection of the same results, not a second
calculation. Snapshot ownership and batching remain separate from presentation: the OBS
collector still issues bulk requests, the SPEC collector uses its isolated worker,
and provider adapters use the bounded monitor runner. A shared result contract
does **not** require calling OBS once per package or a base class with collection
hooks that some monitors cannot implement.

## Add an evidence monitor

Use [monitor_license.py](../backend/tracker/monitor_license.py) for an upgrade-only
port, or [monitor_eol.py](../backend/tracker/monitor_eol.py) for a current-version
port. Both are ordinary modules implementing `monitor.Adapter`; no inheritance,
plugin loader or separate service is needed.

| Member | Contract |
|---|---|
| `TITLE` | Optional display title; defaults to the registry ID. Published with observations, never inferred from a finding label. |
| `VERSION` | Positive interpretation version. Change it when the same inputs would mean different facts; not for every code edit. |
| `HOSTS` | Set of exact provider HTTPS hostnames. Scoped IO rejects other hosts, credentials, redirects and nonstandard ports. |
| `SCOPE` | Optional `current` (default) or `upgrade`. Upgrade jobs only run when the same saved version decision used by the UI is `outdated`. |
| `inputs(package, configured)` | Pure function; return a JSON-compatible dict, or `None` when no reliable identity exists. No network, filesystem or subprocess work. |
| `query_subject(subject, inputs)` | Optional pure dependency projection for `check()`. Defaults to the entire subject; omit only fields that cannot affect the result. |
| `refresh(subject, inputs, previous)` | Optional pure function returning `schedule.Schedule`; default is a six-hour recheck. It may use prior facts and current inputs, but performs no IO. |
| `check(subject, inputs, io)` | Fetch/interpret provider observations; return exactly `status`, `findings`, `note`. Status is `ok`, `partial` or `unsupported`. Raise on failed/invalid provider responses. |

`package` contains `name`, RPM-expanded `version`, source `revision` and the
reviewed native rule's public `identity`. Upgrade context also has `target_version`.
`configured` is only this monitor's `[packages.NAME].monitors.ID` value.
`subject` has the same source fields but no native rule; `inputs` is the resolved
provider-specific identity. An explicit identity takes precedence over derivation.
Use `package_identity.from_native` for supported registry protocols; do not infer
upstream identity from an RPM package prefix or a shared homepage.

Use `io.json(method, url, body=None)` and `io.today`. IO owns HTTP limits, cache,
request deduplication and the operator proxy. A monitor does not create its own
HTTP client, load global configuration, read the snapshot or write state. The
optional fixed cve-bin-tool adapter is a separately constrained subprocess, not
a general executable path available in configuration.

## Facts and failure

Construct results with `monitor_model.finding` and `monitor_model.evidence`.
A finding ID identifies the same assertion across polls; it must not contain a
poll timestamp. Labels/tags are single-word or CamelCase categories, not priority
or action assignments. Evidence carries its source and HTTPS link.

`key` is display text. Use an optional stable `code` when a machine consumer needs
to recognize a field. Security's compact renderer consumes `query`, `fixed_events`
and `epss_probability`; changing their display text must not change selection.
Other codes remain ordinary visible evidence, without a frontend registration.
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
from .schedule import Schedule


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
License use `monitor_model.version_query`: only the current/target version enters
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

Precedence is per-monitor override, explicit legacy `[monitors].interval_seconds`,
then module policy. Remove the legacy global value to use different module defaults;
it is not silently ignored in existing deployments. `stale_after_seconds` must
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

[The Yanked example](../backend/tests/fixtures/monitor_yanked.py) reads the current
release's file observations from the [PyPI JSON API](https://docs.pypi.org/api/json/).
It emits a fact only if all observed release files are yanked. Missing fields fail;
an empty file list is unsupported. This is an offline porting fixture, **not an
enabled production monitor or a claim of live coverage**.

[test_monitor_porting.py](../backend/tests/test_monitor_porting.py) registers it
through the existing registry and runs the actual heartbeat, scoped IO, SQLite
storage, v1/v2 projection, catalog, check-status and Maintenance filters. Provider HTTP is the substituted
boundary. The tests cover findings, empty results, failed requests, source changes,
invalid inputs and isolation from other packages. No scheduler/API/UI branch is
added for the new label.

For a real port:

1. Add `backend/tracker/monitor_ID.py` and register the trusted module once in
   `monitor.REGISTRY` under a stable ID (`source`, `version`, and `build` are reserved).
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
   and a renamed Security finding label to protect this boundary. If a compact
   domain-specific view is needed, add components and one specialization in the
   renderer registry. Select by monitor ID/fact code, never English display text.
5. Inspect one real package with the read-only commands below, then promote the
   configuration and release through the [deployment procedure](deployment.md).
   Observe check statuses/errors and evidence timestamps, not only label counts.

```sh
PYTHONPATH=backend python -m tracker.monitor explain PACKAGE --monitor ID \
  --config config/tracker.toml --db /path/to/snapshot-copy.sqlite3
PYTHONPATH=backend python -m tracker.monitor check PACKAGE --monitor ID \
  --config config/tracker.toml --db /path/to/snapshot-copy.sqlite3
```

`explain` is offline. `check` may contact declared providers but does not write the
snapshot or a production cache. The current contract is for source-version facts;
binary ABI, repository-only health or human dispositions need their own explicit
inputs before they can be truthfully attached. Do not fetch hidden prerequisites
inside the renderer or bypass the source gate to make a port appear covered.

## Change a layout or introduce a new payload kind

For a different homepage arrangement, change `tableLayout`; leave the selector,
GET query normalization and server-side facets alone. The default dense view keeps
Version and per-target Build columns, BuildSystem beside the name and evidence
labels below it. Focusing a monitor promotes its view; uncovered packages remain
visible and can be filtered by check status. Changing focus resets only that
check condition; search, version, build and Maintenance facets stay selected and
retain their cross-monitor meaning. Detail layout uses the same renderer
registry. It does not repeat finding-specific branches in each page.

Use the existing `evidence` payload for sourced assertions. A genuinely different
shape (like Build's target/flavor matrix) requires a typed payload in `api`, its
pure projection in `monitor_views`, regenerated types, and a kind renderer. That
is an intentional schema change, not a reason to introduce untyped JSON or a
runtime plugin loader. Keep the collector's write ownership and failure boundary
explicit; the read model grants no collection privileges.
