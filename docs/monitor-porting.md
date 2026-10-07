# Add a monitor

## Choose the extension point

| Need | Edit | Registration / consumer |
|---|---|---|
| Another package using an existing source | Native version rule or `packages.toml` identity | [Configuration](../config/README.md), without code changes |
| New upstream fact | `monitors/ID.py` | `monitors/registry.py`, then `[monitors].enabled` |
| Another requirements protocol | `monitors/requires/` | `monitor.BACKENDS` |
| Another release-metadata registry | `providers/` | `providers.release.BACKENDS` |
| Missing nvchecker protocol | `backend/nvchecker_source/NAME.py` | Native rule `source`, without monitor registration |
| New presentation of saved facts | `presentation/` | Existing presenter or a new data-kind presenter |

Use [EOL](../backend/tracker/monitors/eol.py) for current-release evidence,
[License](../backend/tracker/monitors/license.py) for upgrade evidence, or
[Yanked](../backend/tracker/monitors/yanked.py) for registry reuse. Evidence adapters reuse the catalog, API, facets and detail renderer.

## Add an evidence adapter

1. Implement the [Adapter contract](../backend/tracker/monitors/contract.py) in
   `monitors/ID.py`. Use a package only if the adapter needs separate files.
2. Register the trusted module in `REGISTRY` and enable it in config. The batch collectors reserve `source`, `version` and `build`. These names cannot identify per-package adapters.
3. Resolve identity with `identity.from_package()`, letting explicit monitor
   settings override saved Source0/native-rule identity. Put package exceptions
   in `[NAME.monitors.ID]`, not Python name branches.
   Released identities belong in `config/packages.toml`. Installation reads that
   catalog from the image. Administrator exceptions use `package_overrides`.
   Replace one complete adapter identity, never merge individual provider fields.
4. Fetch through the supplied `io`. Return attributed facts with `finding()` and
   `evidence()` from `monitors.model`. Reuse existing provider protocols.
   `providers.model.resolve_inputs()` selects explicit registry identities or the first inferred match.
5. Test the runner → storage → API path. Inspect a sample result.
   Release the code and configuration together.

### Module contract

| Member | Contract |
|---|---|
| `VERSION` | Interpretation revision in the fingerprint. Bump when identical inputs acquire different meaning |
| `HOSTS` | Exact HTTPS hosts accepted by scoped IO |
| `TITLE` | Optional display title, otherwise registry ID |
| `SCOPE` | `current` (default), `upgrade`, or `current_and_upgrade` |
| `inputs(package, configured)` | Pure JSON-compatible identity/settings, or `None` for no reliable identity |
| `check(subject, inputs, io)` | Attributed `findings`, `status`, `note` |
| `query_subject(subject, inputs)` | Optional pure fingerprint projection. `check` still receives the full subject |
| `refresh(subject, inputs, previous)` | Optional `Schedule`. Default recheck is six hours |

`package` supplies name/revision, public native-rule `identity`, saved
`source_release` and corroborated full `source_commit` when available. Its version
uses the identified Source0 release, otherwise the source version. `configured`
is this adapter's setting. `subject` contains name/version/revision and, for
upgrade scopes, `target_version`. Adapters receive no snapshot or storage handle.

| Result | Meaning |
|---|---|
| `ok`, including no findings | Provider check completed. This does not establish safety for the whole package |
| `partial` | Usable facts with incomplete subchecks. Preserve failures and their evidence gaps |
| `unsupported` | Identity/metadata cannot support this check. This is not an empty success |
| Exception | Failed/malformed response. The runner isolates it and retains matching dated evidence |

An upgrade-only adapter relies on the shared version decision, not another
comparator. Combined scope returns `scope_checks` for `current` and any `upgrade`.
Each includes `status` (`ok`, `error`, `unsupported`) and `note`.
The overall status can also be `error`. Findings identify their scope and
target. Successful current results survive a failed target check. Unsupported
metadata does not become a transport failure.

Combined scope is for exact upstream releases. `runner.same_scope` matches adapter
revision, resolved inputs and release versions, **not** source revision. Checks
sensitive to local patches must retain source context and use a single scope.

### Facts

Use the ingestion models. Keep finding IDs stable across polls. Labels/tags classify assertions, not urgency or
maintainer actions. Each evidence field carries a provider and public HTTPS URL:

```python
facts = [evidence('Support ended', True, 'Provider', url, code='support_ended')]
return {'status': 'ok', 'findings': [finding('cycle:3', 'EOL', '3.x', facts, url)],
        'note': None}
```

`key` is display text. Use `code` for stable machine identification. Missing values are null with `unavailable`, `not_applicable` or `not_evaluated`,
never false. Reuse issue names from `monitors/issues.py`. Declare styles in
`presentation/labels.toml`.

### IO and scheduling

Use `io.json(method, url, body=None)`, `io.text(url)` and `io.today`. Shared IO owns
bounded transport, cache, deduplication and the operator proxy. IO limits text to 512 KiB. It rejects credentials, redirects, fragments and nonstandard ports.
`min_interval=1.0` paces a host across workers, not cache hits. HTTP 429/503 applies
`Retry-After` with a 60-second minimum. This is a per-collector budget.

```python
from tracker.monitors.schedule import Schedule


def refresh(subject, inputs, previous):
    return Schedule(interval_seconds=21600, retry_seconds=300, max_retry_seconds=3600)
```

Operators set overrides in `[monitors.refresh.ID]`. Staleness must exceed the normal interval.
Keep periodic refresh enabled: vulnerabilities can appear without version changes.
Scheduling, retries and cache-age rules are in [Design](design.md#observation-identity-and-time).

Query fingerprints omit `VERSION`, allowing reinterpretation to retain dated evidence for the same query.
Do not bump `VERSION` for layout-only changes or unrelated backends. Narrow subject
identity only when the omitted field cannot affect a query or its meaning:

| Check | Reusable query identity |
|---|---|
| Exact release metadata | `monitors.model.version_query`: current/target versions |
| Lifecycle | Derived product/cycle |
| Local-patch-sensitive check | Relevant source revision retained |

Fixed security tags must match the queried version. Fixed commits must agree with confined Source0.

## Check the port

[Integration example](../backend/tests/monitors/test_monitor_porting.py) mocks
provider HTTP while exercising runner, SQLite, catalog, projection and facets.

| Fixture | Required result |
|---|---|
| Valid response, with/without a finding | Attributed fact / completed empty result |
| Missing identity, unsupported metadata, timeout | Distinct states. Another package still progresses |
| Same query and facts | Check time advances, evidence revision stays stable |
| New query or in-flight source change | Old evidence cannot become a fresh result for another subject |
| Retry/refresh boundary and cached bytes | Effective schedule and original evidence age preserved |
| Stale or changed upgrade target | No new upgrade check or misplaced finding |
| Partial scope/provider failure | CheckFailed membership agrees with per-monitor filtering and detail Checks |

Inspect offline, then check against a snapshot copy (network allowed, no snapshot
or disk-cache writes):

```sh
PYTHONPATH=backend python -m tracker.monitors explain PACKAGE --monitor ID \
  --config config/tracker.toml --format human
PYTHONPATH=backend python -m tracker.monitors check PACKAGE --monitor ID \
  --config config/tracker.toml --db /path/to/snapshot-copy.sqlite3
```

`explain --db` adds saved context/due status. Run [development checks](../CONTRIBUTING.md#run-checks).
Measure identity coverage, eligible checks, successful checks and findings on the same snapshot.

## Add a Requires backend

Implement `HOSTS`, `inputs(package, configured)` and `read(version, settings, io)`
in `monitors/requires/`, then register in `monitor.BACKENDS`. Return `Requirement`
with stable dependency ID, kind, comparison scheme, declaration/URL and observed
identity, conditions, extras and relationship. Use existing pure comparators in
`requires.compare.COMPARATORS`, or test a new one against accepted/rejected syntax.
Do not branch comparators on dependency names.

Requires observes **upstream declarations**, not SPEC requirements.
Local source versions only assess satisfaction, not installability/ABI. Mappings
come from reviewed native identities and `[openruyi.dependencies]`. Missing or
ambiguous identities remain unknown. Compare current and target declarations only when both are fresh.
Missing data does not prove addition or removal. Local dependency updates can reassess saved facts without
another provider request.

| Backend concern | Preserve |
|---|---|
| PyPI `Requires-Python` / `Requires-Dist` | Markers/extras and target environment, not collector host. Unknown variables remain unknown |
| Optional vs platform condition | Feature selection vs applicability. False target conditions are `not_applicable` |
| crates.io `rust-version` | Build requirement, not runtime dependency |
| CPAN prerequisites | Static `dynamic_config=false`, with distinct requires/recommends/suggests clauses |
| CPAN module versions | Distribution versions cannot substitute for component versions |

Optional `provides(version, settings, io)` returns attributed component versions
for `version_scope="component"` requirements, retained with the successful release
check. Missing/ambiguous components never fall back to distribution versions.
Build declarations remain raw evidence, excluded from list/DepMismatch assessment. Test scope reuse, stale/missing metadata,
mapping and target conditions through the API and presenter.

## Add a registry metadata backend

Implement `HOSTS`, `inputs()` and `metadata()` in `providers/`. Register in
`providers.release.BACKENDS`. Return `Release` with attributed license metadata and
original declaration. Optional `withdrawal()` enables Yanked only when the registry
asserts it. Go uses proxy retraction, not deps.dev's deprecated flag.
Raise `UnsupportedRelease` for absent or ambiguous assertions. Report transport failures as errors.
Neither means no findings. Keep license comparison in the monitor.

## Add a version source

Use native nvchecker sources and options first. For a missing reusable protocol, implement
`async get_version(name, conf, *, cache, **kwargs)` in
`backend/nvchecker_source/NAME.py`. Use nvchecker's cache/session. Keep package identities and release lines in TOML. Use nvchecker for common filtering and normalization.
Provider ordering still matters: Anitya's first stable value is not unordered history.

Extend `identity.from_native()` only if other monitors consume the new identity.
Never import the source plugin into readers. `identity.request_url()` shares URL
resolution with collection. Test the real CLI against loopback fixtures: empty,
malformed, ordering, line changes and cache isolation. The wheel must contain the
plugin. Release its rules with the installed code.

## Change presentation

| Change | Owner |
|---|---|
| Saved facts → typed payload/dimensions | `readmodel/monitors.py` |
| Membership and facet counts | `readmodel/packages.py` |
| Visible facts, order, grouping and links | `presentation/` |
| Layout, themes, keyboard behavior | Astro document components and styles |
| API contract → TypeScript | `api.py`, then `scripts/api-types.py` |

Reuse the evidence renderer. For a different shape, add a typed payload and pure `Presenter`.
Use existing fields, tables and entries before adding a display primitive. Every presenter supplies detail
sections. A Results page needs both `columns` and `cells`. Source is context,
not a duplicate Version table. Collectors never emit HTML or component names.

Aggregate `preview` shows all matching items, folding identical assertions only.
RuntimeDeps shows mismatches, plus changes when selected. Conditions stay in detail.
Build reasons appear beneath their target statuses. The backend supplies filter links and counts. Astro submits GET queries and renders the returned document.
