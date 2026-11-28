# Snapshot data model

This reference explains the fields in Lookout's in-memory `snapshot` dictionary and how collectors populate them.

1. [Snapshot at a glance](#snapshot-at-a-glance)
2. [Shared metadata and conventions](#shared-metadata-and-conventions)
3. [OBS metadata collector](#obs-metadata-collector)
4. [Build status collector](#build-status-collector)
5. [Upstream version collector](#upstream-version-collector)
6. [SPEC collector](#spec-collector)
7. [Monitor runner](#monitor-runner)
8. [GitHub collector](#github-collector)

## Snapshot at a glance

`snapshot` is a Python dictionary containing saved observations and configuration used to interpret them.
For example, `snapshot["sources"]` contains OBS source observations;
`snapshot["specs"]` contains metadata parsed from the packaging repository's SPEC files.

The dictionary below shows the top-level fields after the relevant collectors have published results.
Empty dictionaries and lists stand for omitted contents; `...` stands for an omitted scalar value.
The comments group fields for explanation. All keys belong directly to `snapshot`.
Examples use synthetic packages and illustrate record shapes; linked collector code and models define conditional fields and validation.

```python
snapshot = {
    # Shared metadata and collection health
    "schema": 1,                       # Internal snapshot schema
    "generation": ...,                 # Nonnegative integer identifying a content generation
    "mode": "live",                    # Collection mode
    "components": {},                  # Component ID -> health and checkpoint record

    # OBS metadata collector
    "last_attempt": ...,               # Last OBS metadata attempt time, or None
    "obs": {},                         # OBS project and service URLs
    "targets": [],                     # Build target definitions
    "inventory": {},                   # OBS build object -> source owner name
    "index": {},                       # OBS object -> source revision and hash
    "sources": {},                     # Source owner -> OBS source observation
    "presentation": {},                # Saved distribution settings
    "stale_after_seconds": ...,        # General freshness threshold
    "obs_stale_after_seconds": ...,    # OBS freshness threshold
    "build_history_interval_seconds": ...,  # History refresh interval

    # Build status and history: build and OBS metadata collectors share this field
    "builds": {},                      # OBS build object -> target ID -> build record

    # Upstream version collector; OBS metadata collection also saves bindings/native_ids
    "tracks": {},                      # Version track -> upstream observation
    "bindings": {},                    # Package -> configured track and monitor policy
    "native_ids": [],                  # Available native version rule names
    "nv_digest": ...,                  # Effective version-rule configuration digest

    # SPEC collector
    "specs": {},                       # Packaging directory name -> SPEC observation
    "spec_interval_seconds": ...,      # SPEC refresh interval

    # Monitor runner
    "monitors": {},                    # Package -> evidence adapter ID -> check record
    "monitor_catalog": {},             # Enabled evidence adapter ID -> display title record
    "monitor_stale_after_seconds": ..., # Evidence freshness threshold
    "dependency_packages": {},         # Dependency identity -> source-package name
    "dependency_environments": {},     # Target environment settings for dependency checks

    # GitHub collector
    "github_repositories": [],         # Configured OWNER/REPO identities
    "github_items": {},                # Repository -> item ID -> issue or pull request
    "github_links": {},                # Package -> association key -> matching evidence
}
```

The following sections examine each field group and its nested records in the order
of collection phases in [`collector.py`](../backend/tracker/collector.py): OBS metadata,
build status, upstream versions, SPEC metadata, monitors, and GitHub activity.
The service schedules these phases independently, so a snapshot combines results
observed at different times. See [Design](design.md#persistence-and-publication)
for ownership and publication rules.

## Shared metadata and conventions

### Snapshot metadata

These fields describe the snapshot as a whole:

| Field | Type | Meaning |
|---|---|---|
| `snapshot["schema"]` | Integer | Internal snapshot schema identifier, currently `1`. |
| `snapshot["generation"]` | Nonnegative integer | Advanced when a collector merges its results into the snapshot. |
| `snapshot["mode"]` | String | Collection mode; ordinary collection uses `"live"`. |
| `snapshot["components"]` | Dictionary | Component ID -> collection health and checkpoint record. |

Collection results and collection state have separate locations.
For example, `snapshot["inventory"]` contains OBS build objects;
`snapshot["components"]["inventory"]` records the attempt times and errors of the inventory refresh.
Collector sections describe their component entries, including any saved progress.

### Observation records

An observation is a dictionary within a snapshot collection, such as
`snapshot["sources"][package]` or `snapshot["components"][component]`.
Collectors commonly use the following fields within these records:

| Record field | Meaning |
|---|---|
| `attempted_at` | Time of the last attempt, including failure. |
| `fetched_at` | Successful observation time retained by source, version-track, and component records. |
| `checked_at` | Verification time. OBS sources can reconfirm an unchanged identity; monitors date the inputs supporting their results. |
| `error` | Recorded collection failure, or `None` when no collection error is recorded. |

Timestamp values are timezone-aware ISO 8601 strings. Fields can be absent before
their first observation, and some records allow `None` timestamps.
Not every record uses all four fields.
For monitor-specific timing rules, see [Observation identity and time](design.md#observation-identity-and-time).

## OBS metadata collector

[`collector.collect()`](../backend/tracker/collector.py) gathers OBS metadata;
`collect_obs()` merges its results into the snapshot.
The examples below use a synthetic package, `widget`, and show one target.

### Service scope and targets

```python
snapshot["obs"] = {
    "api_url": "https://build.example.org/public",
    "web_url": "https://build.example.org",
    "project": "example",
}
snapshot["targets"] = [
    {
        "id": "rva23",
        "label": "RISC-V",
        "repository": "riscv64",
        "architecture": "riscv64",
    },
    # Other configured targets omitted.
]
```

`snapshot["obs"]` identifies the OBS project and its API and web endpoints.
Each entry in `snapshot["targets"]` defines a build target.

### Build-object inventory and source index

```python
snapshot["inventory"] = {
    "widget": "widget",
    "widget:tools": "widget",
    # Other build objects omitted.
}
snapshot["index"] = {
    "widget": {"rev": "7", "srcmd5": "..."},
    "widget:tools": {"rev": "7", "srcmd5": "..."},
    # Other source-index entries omitted.
}
```

`snapshot["inventory"]` maps each OBS build object to its source owner.
An ordinary object maps to itself; a flavor such as `widget:tools` maps to `widget`.
Both objects can have build records, while their source observation is stored once
at `snapshot["sources"]["widget"]`.

`snapshot["index"]` contains the bulk source identities reported by OBS.
Each entry has nullable string fields `rev` (OBS revision) and `srcmd5` (source content identity).

### Source observations

`snapshot["sources"]` maps OBS source-owner names to their source observations.
The keys come from the owner names in `snapshot["inventory"]`; flavors share their owner's record.
Each record stores the source identity, observed RPM Version, and collection state.
For example, `snapshot["sources"]["widget"]` describes the OBS source owned by `widget`:

```python
snapshot["sources"] = {
    "widget": {
        "version": "1.2.3",             # Usable RPM Version; None if unresolved
        "raw_version": "1.2.3",         # Original OBS text; may contain macros
        "rev": "7",                     # OBS source revision; may be None
        "srcmd5": "...",                # OBS source content identity; may be None
        "filename": "widget.spec",      # Reported SPEC filename; may be None
        "version_basis": "OBS sourceinfo/version",  # Method used to obtain Version
        "version_error": None,          # Reason Version could not be established
        # Shared observation timestamps and collection error
        "fetched_at": "2026-01-01T10:00:00+00:00",
        "checked_at": "2026-01-01T10:05:00+00:00",
        "attempted_at": "2026-01-01T10:05:00+00:00",
        "error": None,
    },
    # Other source owners omitted.
}
```

For the timestamp and collection-error fields, see [Observation records](#observation-records).
Native fallback can add `native_query` and `native_macro_paths`; its parser provenance
is described under [SPEC collector](#spec-collector).
Optional `unresolved_observation` holds a replacement response whose Version could not be established.

### Saved settings

Alongside observations, this collector saves settings needed to interpret the results:

| Snapshot field | Type | Meaning |
|---|---|---|
| `snapshot["last_attempt"]` | Nullable timestamp string | Start time of the latest OBS metadata attempt. Other phases do not advance it. |
| `snapshot["stale_after_seconds"]` | Integer | General freshness threshold in seconds, used for upstream tracks. |
| `snapshot["obs_stale_after_seconds"]` | Integer | OBS freshness threshold in seconds. Readers fall back to the general threshold when absent. |
| `snapshot["build_history_interval_seconds"]` | Integer | Refresh interval in seconds for successful-build history. |
| `snapshot["presentation"]` | Dictionary | Saved `openruyi` distribution settings used by readers and presenters. |

### Component health and build history

The collector writes health records under `snapshot["components"]`:

| Component path | Check |
|---|---|
| `snapshot["components"]["targets"]` | Configured OBS target validation. |
| `snapshot["components"]["inventory"]` | Complete build-object inventory refresh. |
| `snapshot["components"]["source_index"]` | Bulk source revision/hash index refresh. |
| `snapshot["components"]["build_history:" + target]` | Successful-build history request for the target ID. |

Each uses `attempted_at`, `fetched_at`, and `error` as described in [Observation records](#observation-records).

## Build status collector

`snapshot["builds"]` contains build results for each OBS object listed in `snapshot["inventory"]`.
Under each object, records are keyed by the `id` values in `snapshot["targets"]`.
For example, `snapshot["builds"]["widget:tools"]["rva23"]` describes the `widget:tools` flavor on target `rva23`.

[`collector.collect_builds()`](../backend/tracker/collector.py) fetches project-wide OBS results
and updates status fields. The [OBS metadata collector](#obs-metadata-collector)
independently updates history fields in the same record.

### Build records

```python
snapshot["builds"] = {
    "widget": {
        "rva23": {
            # Written by the build status collector
            "raw_status": "failed",     # OBS result code
            "details": "compile error", # OBS result details; may be empty
            "matches_source": None,     # OBS parser writes None; the read model derives correspondence
            "fetched_at": "2026-01-01T10:05:00+00:00",
            "attempted_at": "2026-01-01T10:05:00+00:00",
            "error": None,              # Status was collected successfully

            # Written by the OBS metadata collector
            "history_attempted_at": "2026-01-01T10:00:00+00:00",
            "history_checked_at": "2026-01-01T10:00:00+00:00",
            "history_error": None,      # History collection error, if any
            "history_unresolved": False, # True when job reuse obscures the original success
            "last_success": {
                "version": "1.2.3",     # Successful job's RPM Version; may be None
                "time": "2026-01-01T09:00:00+00:00",  # Job completion time
                "srcmd5": "...",        # Source identity for that successful job
                "build_identity": {     # Present when OBS supplies revision and count
                    "rev": "7",         # OBS source revision
                    "bcnt": "2",        # OBS build counter
                    "versrel": "1.2.3-1", # Original VERSION-RELEASE text
                },

                # Optional fields from a historical RPM artifact lookup
                "version_basis": "OBS RPM fileinfo/version",
                "version_error": None, # Reason artifact Version could not be resolved
                "binary_provenance": {
                    "filename": "widget-1.2.3-1.2.src.rpm",
                    "release": "1.2",  # Artifact RPM Release
                    "architecture": "src",
                    "repository": "riscv64",
                    "target_architecture": "riscv64",
                    "srcmd5": "...",   # Verified historical source identity
                    "mtime": None,     # Artifact Unix timestamp, or None
                },
            },

            # Optional lookup state for the last successful build
            "version_attempt_identity": [
                "...",                 # srcmd5
                "2026-01-01T09:00:00+00:00",  # Successful job completion time
                "7", "2", "1.2.3-1",  # rev, bcnt, versrel
            ],
            "version_attempted_at": "2026-01-01T10:00:00+00:00",
        },
        # Other targets omitted.
    },
    # Other build objects omitted.
}
```

An OBS result of `"failed"` describes the build outcome.
It can coexist with `error=None`, as above, when collecting that result succeeded.
A request failure or missing target/object result instead sets `error` and retains earlier status facts.
Its poll state is recorded in `snapshot["components"]["builds"]`.
See [Display contracts](design.md#display-contracts) for build outcomes versus collection failures.

Within this same record, `snapshot["builds"]["widget"]["rva23"]["last_success"]`
retains the latest known successful job even when a later build fails.
Its `time` is job completion time; `history_checked_at` dates the check that obtained the history.
`last_success` can be absent or `None` when no success has been established.

## Upstream version collector

[`collector.check_upstreams()`](../backend/tracker/collector.py) runs native nvchecker
rules and publishes their results in `snapshot["tracks"]`.
Each track is a named rule that selects an upstream version or commit.
A package's saved binding identifies which track to compare with its current source.

### Package bindings and track names

`snapshot["bindings"]` maps configured package names to comparison and monitor policy.
`snapshot["native_ids"]` lists the available native rule names, including tracks whose results have not arrived yet.
Both the OBS metadata and upstream collectors save these fields.

```python
snapshot["bindings"] = {
    "widget": {
        "compare": "widget@2",          # Main version track; may be None
        "watch": ["widget@preview"],    # Additional tracks shown alongside it
        "track_label": "2.x",          # Display label for the package's release line
        "comparable": True,             # Optional comparison policy; defaults to True
        "not_applicable": False,        # Optional policy; defaults to False
        # Per-adapter monitors policy may also be present.
    },
    # Other configured packages omitted.
}
snapshot["native_ids"] = [
    "widget@2",
    "widget@preview",
    # Other native rule names omitted.
]
snapshot["nv_digest"] = "..."           # Digest of the effective version-rule configuration
```

Here, `snapshot["bindings"]["widget"]["compare"]` contains the track name `"widget@2"`.
Readers use the upstream version in `snapshot["tracks"]["widget@2"]` to compare with `widget`'s current source version.
The watch track is an additional observation; it does not replace the main comparison.
Package names and track names can therefore differ.

### Track observations

`snapshot["tracks"]` maps native rule names to their latest saved observations.
The record below represents a successful release-version check:

```python
snapshot["tracks"] = {
    "widget@2": {
        "version": "2.4.0",             # Last successful checker result
        "source": {                    # Public provider identity from the native rule
            "source": "pypi",
            "pypi": "widget",
        },
        "configuration_fingerprint": "...", # Identity of the rule used for this attempt
        "reported_at": "2026-01-01T10:00:00+00:00", # Last run reporting this track's result/error
        "source_checked_at": None,      # Current successful imports set this to None
        "failures": 0,                  # Consecutive failures used for retry scheduling
        "fetched_at": "2026-01-01T10:00:00+00:00",
        "attempted_at": "2026-01-01T10:00:00+00:00",
        "error": None,
    },
    # Other tracks, including widget@preview, omitted.
}
```

[`monitors/version/nvchecker.py`](../backend/tracker/monitors/version/nvchecker.py)
imports these records from checker events. `source` contains the public subset of
the rule's provider identity and supported selection options.
Some providers also supply optional `revision` (a full Git commit hash) and
`revision_creation_time` (the provider's commit timestamp, or `None`).
These fields can accompany release checks as well as commit-tracking rules.
A track can lack `version` and `fetched_at` before its first successful check.
The aggregate collection record is `snapshot["components"]["nvchecker"]`;
[`check_upstreams()`](../backend/tracker/collector.py) defines its due-pass counts and timestamp aggregation.
For rule configuration and default bindings, see [Version rules](../config/README.md#version-rules).

## SPEC collector

`snapshot["specs"]` maps packaging directory names to parsed SPEC observations.
For example, `snapshot["specs"]["widget"]` describes `SPECS/widget/widget.spec`
in the configured packaging Git repository.
[`collector.check_specs()`](../backend/tracker/collector.py) refreshes that repository
and parses the selected packages through `refresh_specs()`.

### Package records

```python
snapshot["specs"] = {
    "widget": {
        "head": "...",                 # Recorded packaging commit for this package
        "metadata": {                  # Expanded SPEC fields; None before a successful parse
            "name": "widget",          # RPM Name; may differ from the directory name
            "version": "1.2.3",        # RPM Version
            "summary": "Example package",
            "description": "An example package for this reference.",
            "license": "MIT",
            "url": "https://example.org/widget",
            "buildsystem": "autotools", # Declared BuildSystem; may be None
            "go_module": None,          # Parsed Go module identity, when available
            "sources": [
                {"number": 0, "url": "https://example.org/widget/widget-1.2.3.tar.xz"},
                {"number": 1, "url": "config.inc"},  # Source values can be local filenames
                # Other Source entries omitted.
            ],
        },
        "changelog": [                 # Packaging Git history, newest first
            {
                "commit": "...",
                "date": "2026-01-01T09:00:00+00:00", # Git author date
                "author": "Example Maintainer",
                "subject": "Update widget to 1.2.3",
                "signed_off_by": [],   # Signed-off-by trailer values
            },
            # Older packaging commits omitted.
        ],
        "source_origin": {             # Packaging repository identity
            "url": "https://github.com/example/packaging.git",
            "branch": "main",
            "source_url_template": None, # Optional template for source browsing links
        },
        "native_query": {              # Inputs and environment of the native RPM parser
            "spec_sha256": "...",      # Digest of the SPEC bytes supplied to the parser
            "error": None,             # Native parser error, if any
            "context": {
                "resolver": ...,       # Parser interpretation revision
                "rpm": "...",          # RPM runtime version
                "target": "x86_64",    # Parser target CPU
                "rpm_target": "x86_64-linux",
                "environment": "one-shot Landlock+seccomp native RPM worker",
                "additional_macros": [
                    {"path": "SPECS/rpm-config-example/macros", "sha256": "..."},
                    # Other pinned macro inputs omitted.
                ],
                "local_sources": [     # Optional explicitly configured package inputs
                    {"path": "SPECS/widget/config.inc", "name": "config.inc", "sha256": "..."},
                ],
                "sandbox": {           # Isolation identity; absent if not established
                    "policy": 1,
                    "landlock_abi": ...,
                    "seccomp": "allow-list",
                    "memory_bytes": ...,
                    "process_limit": ...,
                },
            },
        },
        "fetched_at": "2026-01-01T10:00:00+00:00",
        "attempted_at": "2026-01-01T10:00:00+00:00",
        "error": None,
    },
    # Other packaging directories omitted.
}
snapshot["spec_interval_seconds"] = ... # Configured SPEC refresh interval in seconds
```

The `sources` array preserves Source numbers from the SPEC; `number=0` identifies Source0.
The `changelog` array contains bounded packaging Git history, rather than RPM's textual `%changelog`.

The package's `head` normally identifies its newest packaging changelog entry.
`native_query` is produced by [`monitors/source/rpm.py`](../backend/tracker/monitors/source/rpm.py).
Its context identifies the parser and pinned inputs; the target CPU describes parsing, not a completed build.

OBS native-parser fallback uses the same `native_query` shape, with OBS-pinned macro paths.
For isolation guarantees and limits, see [Native and network boundaries](design.md#native-and-network-boundaries).

Repository progress is saved in `snapshot["components"]["spec_git"]` by
[`check_specs()`](../backend/tracker/collector.py).
[`state.package_names()` and `state.current_source()`](../backend/tracker/state.py)
define catalog membership and source selection using the saved SPEC and OBS records.
For failure retention and offline reuse, see [Persistence and publication](design.md#persistence-and-publication).

## Monitor runner

`snapshot["monitors"]` maps package names to evidence adapter records, such as `eol`, `security`, or `requires`.
For example, `snapshot["monitors"]["widget"]["eol"]` records the lifecycle check for package `widget`.


### Check records and findings

Each check records its subject, resolved adapter inputs, execution state, and findings.
Each finding contains provider-attributed facts supporting that result:

```python
snapshot["monitors"] = {
    "widget": {
        "eol": {
            "subject": {               # Source identity used for this check
                "name": "widget",
                "version": "1.2.3",
                "revision": "spec:...",
            },
            "scope": "current",        # Check the current release
            "inputs": {                # Resolved identity/settings; adapter-specific
                "product": "widget",
                "cycle_parts": 2,
            },
            "adapter_version": ...,    # Adapter interpretation revision
            "fingerprint": "...",      # Query identity including adapter revision
            "query_fingerprint": "...", # Query identity excluding adapter revision
            "input_status": "pending", # Eligible for execution
            "input_note": None,        # Explanation of an eligibility restriction
            "status": "ok",            # Result of the check
            "note": None,              # Planning or result explanation
            "error": None,             # Isolated failure category, if any
            "attempted_at": "2026-01-01T10:00:00+00:00",
            "checked_at": "2026-01-01T10:00:00+00:00",
            "failures": 0,             # Retry counter for failed or partial checks
            "evidence_revision": "...", # Hash of normalized query/evidence contents
            "changed_at": "2026-01-01T10:00:00+00:00",
            "findings": [
                {
                    "id": "cycle:1.2", # Stable identity within this adapter
                    "label": "EOL",    # Classification identifier
                    "title": "Widget 1.2 support ended",
                    "scope": "current",
                    "target_version": None, # Upgrade findings identify a target Version
                    "tags": [],        # Additional classification identifiers
                    "evidence_url": "https://example.org/widget/lifecycle",
                    "facts": [
                        {
                            "key": "Support ended", # Display text
                            "code": "support_ended", # Optional machine identifier
                            "value": True,          # Provider assertion
                            "source": "Example Provider",
                            "url": "https://example.org/widget/lifecycle",
                            "status": "observed",
                        },
                        # Other supporting facts omitted.
                    ],
                },
                # Other findings omitted.
            ],
        },
        # Other evidence adapters omitted.
    },
    # Other packages omitted.
}
```

`input_status` describes eligibility; `status` describes the planned or completed check.
Thus, `input_status="pending"` can coexist with `status="ok"` after successful execution.
`inputs` can be `None` when no reliable identity is available or the adapter is disabled.
Timing and result fields can be absent before execution.
For subject selection, scopes, and status meanings, see [Module contract](monitor-porting.md#module-contract).

The finding and fact structures are defined by `RawFinding` and `Evidence` in
[`monitors/model.py`](../backend/tracker/monitors/model.py).
See [Facts](monitor-porting.md#facts) for value/status conventions and evidence attribution.

Dependency findings can additionally contain a `requirement` object describing an upstream declaration.
Its fields and defaults are defined by `RequirementDeclaration` in
[`requires/model.py`](../backend/tracker/monitors/requires/model.py).
For declaration and distribution-assessment semantics, see [Requires backends](monitor-porting.md#add-a-requires-backend).

### Release scopes and retained results

Combined adapters add `scope_checks` within their check record.
This excerpt shows a successful current-release check and a failed upgrade check;
scope contracts are defined in [Monitor porting](monitor-porting.md#module-contract).

```python
snapshot["monitors"]["widget"]["requires"] = {
    "subject": {
        "name": "widget",
        "version": "1.2.3",
        "revision": "spec:...",
        "target_version": "2.4.0",
    },
    "scope": "current_and_upgrade",
    "status": "partial",
    "scope_checks": {
        "current": {
            "status": "ok",
            "note": None,
            "attempted_at": "2026-01-01T10:00:00+00:00",
            "checked_at": "2026-01-01T10:00:00+00:00",
        },
        "upgrade": {
            "status": "error",
            "note": "Upstream release requirements could not be read.",
            "attempted_at": "2026-01-01T10:00:00+00:00",
            "checked_at": None,
        },
    },
    # Other check fields omitted.
}
```

An optional scope-level `provides` array records component identities (`ecosystem`, `name`),
versions, provider names, and URLs for the exact release.
The shape is defined by `ProvidedComponent` in [`requires/model.py`](../backend/tracker/monitors/requires/model.py).

Optional `last_result` within an adapter record holds its previous completed observation,
including its own subject, fingerprints, findings, and timestamps.
Its selection rules are defined in [Observation identity and time](design.md#observation-identity-and-time)
and implemented in [`monitors/observations.py`](../backend/tracker/monitors/observations.py).

Read-time states such as `input_changed`, `expired`, and `schema_changed` are derived from these records.
They need not match the saved `status` string.

### Saved catalog and dependency settings

The runner also publishes the settings needed to interpret the saved evidence:

```python
snapshot["monitor_catalog"] = {
    "eol": {"title": "EOL"},            # Enabled adapter ID -> display title record
    "requires": {"title": "Dependencies"},
    # Other enabled adapters omitted.
}
snapshot["monitor_stale_after_seconds"] = ... # Evidence freshness threshold in seconds
snapshot["dependency_packages"] = {
    "python": "python",                # Dependency identity -> distribution source package
    # Other dependency mappings omitted.
}
snapshot["dependency_environments"] = {
    "pep508": {                        # Explicit target facts for dependency marker evaluation
        "sys_platform": "linux",
        "os_name": "posix",
        # Other configured target facts omitted.
    },
}
```

The runner stores per-package check health in `snapshot["monitors"]`;
it does not create per-adapter records in `snapshot["components"]`.
For dependency mappings and target settings, see [Configuration](../config/README.md).

## GitHub collector

[`monitors/github/collector.py`](../backend/tracker/monitors/github/collector.py)
collects issues and pull requests from configured repositories.
`snapshot["github_repositories"]` lists those repositories;
`snapshot["github_items"]` stores their activity records.
`snapshot["github_links"]` connects activity records to packages using saved matching evidence.

### Repository activity records

`snapshot["github_items"]` maps each `OWNER/REPO` identity to records keyed by GitHub item ID.
The ID is stored as a string and differs from the repository-local issue or PR number.
For example, the item below has ID `"1001"` and PR number `42`:

```python
snapshot["github_repositories"] = [
    "example/packaging",
    # Other configured repositories omitted.
]
snapshot["github_items"] = {
    "example/packaging": {
        "1001": {
            "repository": "example/packaging",
            "id": "1001",              # GitHub item ID, matching the dictionary key
            "number": 42,              # Repository-local issue or PR number
            "kind": "pr",              # "pr" or "issue"
            "url": "https://github.com/example/packaging/pull/42",
            "title": "Update package metadata",
            "body": None,              # Provider text; may be None
            "author": "contributor",   # GitHub login; may be None
            "state": "open",           # Provider state: "open" or "closed"
            "state_reason": None,      # Provider's closure reason
            "status": "open",          # Normalized issue/PR state
            "labels": ["maintenance"],
            "created_at": "2026-01-01T09:00:00Z",
            "updated_at": "2026-01-01T09:30:00Z",
            "closed_at": None,
            "merged_at": None,         # PR only
            "paths": [                 # Changed paths, including old names for renames
                "SPECS/helper/helper.spec",
                "SPECS/widget/widget.spec",
                # Other changed paths omitted.
            ],
            "paths_complete": True,    # Whether the changed-file list is complete
            "diff_key": ["...", "..."], # PR only: base and head Git commit identities
            "source_key": "...",       # Fingerprint of normalized text, labels, and kind
            "available": True,         # False if absent from a completed full reconciliation
        },
        # Other issues and pull requests omitted.
    },
    # Other repositories omitted.
}
```

For PRs, `status` can be `open`, `draft`, `closed`, or `merged`.
For issues, it can be `open`, `closed`, `completed`, or `not_planned`.
Issue records have an empty `paths` array and omit the PR-only fields.
The timestamps come from GitHub; they describe the item rather than the collector's polling times.

A completed full reconciliation marks previously saved items absent from the response as `available=False`.
Their records remain available for historical display.
PR file lists are published only after confirming the base/head identities;
`paths_complete=False` preserves an incomplete list explicitly.

### Package associations

`snapshot["github_links"]` maps package names to matching activity items.
Each association key combines the repository and item ID as `OWNER/REPO#id`:

```python
snapshot["github_links"] = {
    "widget": {
        "example/packaging#1001": {
            "repository": "example/packaging",
            "id": "1001",              # References github_items[repository][id]
            "evidence": [
                {"kind": "changed_path", "value": "SPECS/widget/widget.spec"},
                # Other matching evidence omitted.
            ],
        },
        # Other associations for widget omitted.
    },
    "helper": {
        "example/packaging#1001": {
            "repository": "example/packaging",
            "id": "1001",              # Same activity item, associated with another package
            "evidence": [
                {"kind": "changed_path", "value": "SPECS/helper/helper.spec"},
            ],
        },
        # Other associations for helper omitted.
    },
    # Other packages omitted.
}
```

Both packages reference the same item under `snapshot["github_items"]["example/packaging"]["1001"]`.
The item is stored once, while each package retains its own association evidence.
`kind` can identify a changed path, a name or path in the title/body, or a package label.
These `kind`/`value` entries describe matching reasons; they differ from monitor finding facts.
The matching rules are implemented by `Matcher` in
[`monitors/github/model.py`](../backend/tracker/monitors/github/model.py).

### Repository checkpoints

`snapshot["components"]["github:" + repository]` stores the repository's collection health and progress.
It allows the next pass to resume pagination or wait for a provider cooldown:

```python
snapshot["components"]["github:example/packaging"] = {
    "attempted_at": "2026-01-01T10:00:00+00:00",
    "fetched_at": "2026-01-01T09:00:00+00:00", # Last completed refresh
    "error": None,
    "etag": "...",                     # Conditional validator for the update probe
    "since": "2026-01-01T08:58:00+00:00", # Provider-time cursor, with overlap
    "reconciled_at": "2026-01-01T09:00:00+00:00", # Last completed full reconciliation
    "next_poll_at": "2026-01-01T10:10:00+00:00",
    "retry_at": None,                  # Provider cooldown deadline, when present
    "poll_delay": 600,                 # Adaptive polling delay in seconds
    "stale_after_seconds": 1800,       # Repository-specific freshness threshold
    "authenticated": False,            # Whether the client has active token authentication
    "auth_retry_at": 0,                # Authentication retry deadline as Unix seconds

    # Optional progress fields while a synchronization round is unfinished
    "page": 2,                         # Current issue-list page
    "round_started": "2026-01-01T10:00:00+00:00",
    "round_watermark": "2026-01-01T09:30:00Z", # Provider update time captured at round start
    "full": False,                     # True for full reconciliation
    "seen": ["1001"],                  # IDs seen across completed pages; others omitted
    "processed": [],                   # IDs processed in the current page
    "latest_updated": "2026-01-01T09:30:00+00:00",
    "changed": True,                   # Whether this round changed saved items
    "batch": [                         # Raw GitHub issue-list page
        # Provider entries omitted.
    ],
    "pr_progress": {                   # Optional in-progress PR detail/file lookup
        "source_key": "...",
        "detail": {},                  # Raw PR response; contents omitted
        "paths": ["SPECS/widget/widget.spec"], # Other collected paths omitted
        "count": 100,                  # File entries processed so far
        "file_page": 2,                # Next file-list page
        "files_done": True,            # File pages finished; endpoint confirmation still pending
    },
}
```

Checkpoint fields are optional until the corresponding stage has run.
`auth_retry_at=0` means no saved authentication cooldown; credentials are not stored here.
`page` indicates an unfinished round. Completed rounds remove temporary fields such as
`page`, `batch`, `processed`, and `pr_progress`.

A request-budget pause can leave `error=None` while work remains incomplete.
Provider cooldowns set `retry_at`; rejected requests can also record an error.
Previously collected items remain available during these pauses and failures.
[`readmodel/activity.py`](../backend/tracker/readmodel/activity.py) combines item completeness
and repository checkpoint state to derive activity coverage, counts, and pages.
One repository error can therefore affect the activity check for many packages.
