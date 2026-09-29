# Add a monitor

## Choose the extension point

| Need | Edit | Registration / consumer |
|---|---|---|
| Another package using an existing source | Native version rule or `packages.toml` identity | [Configuration](../config/README.md); no code |
| New upstream fact | `monitors/ID.py` | `monitors/registry.py`, then `[monitors].enabled` |
| Another requirements protocol | `monitors/requires/` | `monitor.BACKENDS` |
| Another release-metadata registry | `providers/` | `providers.release.BACKENDS` |
| Missing nvchecker protocol | `backend/nvchecker_source/NAME.py` | Native rule `source`; no monitor registration |
| New presentation of saved facts | `presentation/` | Existing presenter or a new data-kind presenter |

Use [EOL](../backend/tracker/monitors/eol.py) for current-release evidence,
[License](../backend/tracker/monitors/license.py) for upgrade evidence, or
[Yanked](../backend/tracker/monitors/yanked.py) for registry reuse. A normal evidence
adapter gets catalog, API, facets and detail presentation automatically; it needs
no Astro branch or new API endpoint.

## Add an evidence adapter

1. Implement the [Adapter contract](../backend/tracker/monitors/contract.py) in
   `monitors/ID.py`. Use a package only when responsibilities warrant separate files.
2. Register the trusted module in `REGISTRY` and enable it in config. `source`,
   `version` and `build` are reserved batch collectors, not per-package adapters.
3. Resolve identity with `identity.from_package()`, letting explicit monitor
   settings override saved Source0/native-rule identity. Put package exceptions
   in `[NAME.monitors.ID]`, not Python name branches.
4. Fetch through supplied `io`; return attributed facts using `finding()` and
   `evidence()` from `monitors.model`. Reuse existing provider protocols.
5. Test the runner → storage → API path, inspect a sample and release code with config.

### Module contract

| Member | Contract |
|---|---|
| `VERSION` | Interpretation revision in the fingerprint; bump when identical inputs acquire different meaning |
| `HOSTS` | Exact HTTPS hosts accepted by scoped IO |
| `TITLE` | Optional display title, otherwise registry ID |
| `SCOPE` | `current` (default), `upgrade`, or `current_and_upgrade` |
| `inputs(package, configured)` | Pure JSON-compatible identity/settings, or `None` for no reliable identity |
| `check(subject, inputs, io)` | Attributed `findings`, `status`, `note` |
| `query_subject(subject, inputs)` | Optional pure fingerprint projection; `check` still receives the full subject |
| `refresh(subject, inputs, previous)` | Optional `Schedule`; default recheck is six hours |

`package` supplies name/revision, public native-rule `identity`, saved
`source_release` and corroborated full `source_commit` when available. Its version
uses the identified Source0 release, otherwise the source version. `configured`
is this adapter's setting. `subject` contains name/version/revision and, for
upgrade scopes, `target_version`. Adapters receive no snapshot or storage handle.

| Result | Meaning |
|---|---|
| `ok`, including no findings | Provider check completed; not a claim that the whole package is safe |
| `partial` | Usable facts with incomplete subchecks; preserve failures and their evidence gaps |
| `unsupported` | Identity/metadata cannot support this check; not an empty success |
| Exception | Failed/malformed response; runner isolates it and retains matching dated evidence |

An upgrade-only adapter relies on the shared version decision, not another
comparator. Combined scope additionally returns `scope_checks` for `current` and,
when present, `upgrade`, each with `status` (`ok`, `error`, `unsupported`) and
`note`. Its overall status may also be `error`. Findings identify their scope and
target. Successful current results survive a failed target check; unsupported
metadata does not become a transport failure.

Combined scope is for exact upstream releases. `runner.same_scope` matches adapter
revision, resolved inputs and release versions, **not** source revision. Checks
sensitive to local patches must retain source context and use a single scope.

### Facts

Use the ingestion models, not a second schema. Finding IDs persist across polls;
poll time is not identity. Labels/tags classify assertions, not urgency or
maintainer actions. Each evidence field carries a provider and public HTTPS URL:

```python
facts = [evidence('Support ended', True, 'Provider', url, code='support_ended')]
return {'status': 'ok', 'findings': [finding('cycle:3', 'EOL', '3.x', facts, url)],
        'note': None}
```

`key` is display text; `code` is a stable machine identifier when a consumer needs
one. Missing values are null with `unavailable`, `not_applicable` or `not_evaluated`,
never false. Reuse issue names from `monitors/issues.py`; declare styles in
`presentation/labels.toml`. The generic renderer distinguishes outline issue tags
from solid BuildSystem identities.

### IO and scheduling

Use `io.json(method, url, body=None)`, `io.text(url)` and `io.today`. Shared IO owns
bounded transport, cache, deduplication and the operator proxy. Text is limited to
512 KiB. Credentials, redirects, fragments and nonstandard ports are rejected.
`min_interval=1.0` paces a host across workers, not cache hits. HTTP 429/503 applies
`Retry-After` with a 60-second minimum; this is a per-collector budget.

```python
from tracker.monitors.schedule import Schedule


def refresh(subject, inputs, previous):
    return Schedule(interval_seconds=21600, retry_seconds=300, max_retry_seconds=3600)
```

Changed fingerprints are immediately eligible; errors/partials use capped
exponential retries. Batch/worker limits may delay eligible work. Cache age is
bounded by the effective interval. Operators override policies in
`[monitors.refresh.ID]`; staleness must exceed the normal interval. Vulnerabilities
can appear without version changes, so periodic refresh remains necessary.

Result fingerprints include adapter ID, `VERSION` and inputs; query fingerprints
omit `VERSION` so reinterpretation can retain dated same-query evidence. Do not
bump `VERSION` for layout-only changes or unrelated new backends. Narrow subject
identity only when the omitted field cannot affect a query or its meaning:

| Check | Reusable query identity |
|---|---|
| Exact release metadata | `monitors.model.version_query`: current/target versions |
| Lifecycle | Derived product/cycle |
| Local-patch-sensitive check | Relevant source revision retained |

A successful poll advances check time, not unchanged evidence revision. Cached
bytes keep their original time. Source unavailability gates evidence; it does not
freshen it. Persistence and publication details are in [Design](design.md).

## Verify the port

[Integration example](../backend/tests/monitors/test_monitor_porting.py) mocks
provider HTTP while exercising runner, SQLite, catalog, projection and facets.

| Fixture | Required result |
|---|---|
| Valid response, with/without a finding | Attributed fact / completed empty result |
| Missing identity, unsupported metadata, timeout | Distinct states; another package still progresses |
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
Measure identity, eligible and successful-check coverage separately from findings,
using the same snapshot; few alerts do not imply little coverage.

## Add a Requires backend

Implement `HOSTS`, `inputs(package, configured)` and `read(version, settings, io)`
in `monitors/requires/`, then register in `monitor.BACKENDS`. Return `Requirement`
with stable dependency ID, kind, comparison scheme, declaration/URL and observed
identity, conditions, extras and relationship. Use existing pure comparators in
`requires.compare.COMPARATORS`, or test a new one against accepted/rejected syntax.
Do not branch comparators on dependency names.

Requires observes **upstream declarations**; SPEC requirements are not a substitute.
Local source versions only assess satisfaction, not installability/ABI. Mappings
come from reviewed native identities and `[openruyi.dependencies]`; missing or
ambiguous identities remain unknown. Current/target declarations are independent:
a missing side does not prove addition/removal; changes require two fresh observed
unequal declarations. Local dependency updates can reassess saved facts without
another provider request.

| Backend concern | Preserve |
|---|---|
| PyPI `Requires-Python` / `Requires-Dist` | Markers/extras; target environment, not collector host; unknown variables remain unknown |
| Optional vs platform condition | Feature selection vs applicability; false target conditions are `not_applicable` |
| crates.io `rust-version` | Build requirement, not runtime dependency |
| CPAN prerequisites | Static `dynamic_config=false`; requires/recommends/suggests as distinct clauses |
| CPAN module versions | Distribution versions cannot substitute for component versions |

Optional `provides(version, settings, io)` returns attributed component versions
for `version_scope="component"` requirements, retained with the successful release
check. Missing/ambiguous components never fall back to distribution versions.
Build declarations remain raw evidence, outside list/DepMismatch assessment until
a reliable build-dependency monitor exists. Test scope reuse, stale/missing metadata,
mapping and target conditions through the API and presenter.

## Add a registry metadata backend

Implement `HOSTS`, `inputs()` and `metadata()` in `providers/`; register in
`providers.release.BACKENDS`. Return `Release` with attributed license metadata and
original declaration. Optional `withdrawal()` enables Yanked only when the registry
asserts it; Go uses proxy retraction, not deps.dev's deprecated flag.
Raise `UnsupportedRelease` for absent/ambiguous assertions; transport failures remain
errors. Neither is a negative finding. License comparison stays in the monitor.

## Add a version source

Prefer native nvchecker sources/options. A missing reusable protocol can implement
`async get_version(name, conf, *, cache, **kwargs)` in
`backend/nvchecker_source/NAME.py`; use nvchecker's cache/session. TOML owns package
identities and release lines; nvchecker owns common filtering/normalization.
Provider ordering still matters: Anitya's first stable value is not unordered history.

Extend `identity.from_native()` only if other monitors consume the new identity;
never import the source plugin into readers. `identity.request_url()` shares URL
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

The common evidence shape needs no new renderer. A genuinely different shape needs
a typed payload and pure `Presenter`, using fields/tables/entries first. Add a
primitive only when those cannot express it. Every presenter supplies detail
sections; a Results page needs both `columns` and `cells`. Source is context,
not a duplicate Version table. Collectors never emit HTML or component names.

Aggregate `preview` shows all matching items, folding identical assertions only.
RuntimeDeps shows mismatches, plus changes when selected; conditions stay in detail.
Build reasons appear beneath their target statuses. The backend owns filter links
and counts; Astro submits the GET query and renders the returned document.
