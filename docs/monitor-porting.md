# Port a monitor

A monitor adds a sourced fact about a package, not another way to choose its
current/upstream version. `version_status` owns that decision. OBS build evidence
and SPEC source evidence keep their separate provenance.

## What to implement

Use [monitor_license.py](../backend/tracker/monitor_license.py) for an upgrade-only
port, or [monitor_eol.py](../backend/tracker/monitor_eol.py) for a current-version
port. Both are ordinary modules implementing `monitor.Adapter`; no inheritance,
plugin loader or separate service is needed.

| Member | Contract |
|---|---|
| `VERSION` | Positive interpretation version. Change it when the same inputs would mean different facts; not for every code edit. |
| `HOSTS` | Set of exact provider HTTPS hostnames. Scoped IO rejects other hosts, credentials, redirects and nonstandard ports. |
| `SCOPE` | Optional `current` (default) or `upgrade`. Upgrade jobs only run when the same saved version decision used by the UI is `outdated`. |
| `inputs(package, configured)` | Pure function; return a JSON-compatible dict, or `None` when no reliable identity exists. No network, filesystem or subprocess work. |
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

The runner owns input fingerprints, retry intervals, batching, provider fairness,
snapshot publication and exact-input failure retention. An `inputs()` exception
is local to that package/monitor; a `check()` exception retains only matching old
evidence without refreshing its successful-check time. Changed source revisions
or upgrade targets invalidate old evidence. An idle heartbeat writes nothing.

## Executable example and acceptance

[The Yanked example](../backend/tests/fixtures/monitor_yanked.py) reads the current
release's file observations from the [PyPI JSON API](https://docs.pypi.org/api/json/).
It emits a fact only if all observed release files are yanked. Missing fields fail;
an empty file list is unsupported. This is an offline porting fixture, **not an
enabled production monitor or a claim of live coverage**.

[test_monitor_porting.py](../backend/tests/test_monitor_porting.py) registers it
through the existing registry and runs the actual heartbeat, scoped IO, SQLite
storage, API projection and Maintenance filter. Provider HTTP is the substituted
boundary. The tests cover findings, empty results, failed requests, source changes,
invalid inputs and isolation from other packages. No scheduler/API/UI branch is
added for the new label.

For a real port:

1. Add `backend/tracker/monitor_ID.py` and register the trusted module once in
   `monitor.REGISTRY`. Add your provider fixtures and tests alongside the example.
2. Enable `ID` in `[monitors].enabled`. Add per-package identity exceptions only
   where `inputs()` cannot derive a reviewed identity. Never put scripts or
   credentials in these identities.
3. Run the existing [development and native checks](../CONTRIBUTING.md). Include
   wrong/missing identity, malformed responses, no finding, timeout, unchanged
   evidence and changed inputs. Upgrade ports also test no upgrade, disabled
   comparison, stale upstream and changed target through the shared runner.
4. Inspect one real package with the read-only commands below, then promote the
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
