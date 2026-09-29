# Monitor configuration

## Find the owner

| Change | File / table |
|---|---|
| Upstream identity, release filter, version line | [`versions/nvchecker.toml`](versions/nvchecker.toml), by track name |
| Compare/watch policy or monitor identity exception | [`packages.toml`](packages.toml), by package name |
| OBS targets, SPEC repository, intervals, enabled monitors | [`tracker.toml`](tracker.toml) |
| BuildSystem colors and icons | `[openruyi.buildsystems]`; icons reference the local asset catalog |
| Dependency → source-package mappings | `[openruyi.dependencies]` |
| Dependency target environments | `[openruyi.dependency_environments]` |

`collector.nvchecker_config` and `packages_config` resolve relative to
`tracker.toml`. Only explicitly referenced files are loaded. Keep operator config
and credentials outside the checkout; see [deployment](../docs/deployment.md).

Locate the effective rule and identity **without contacting providers**:

```sh
PYTHONPATH=backend python -m tracker.package explain python-requests \
  --config config/tracker.toml --format human
PYTHONPATH=backend python -m tracker.monitors explain openssl --monitor eol \
  --config config/tracker.toml --format human
```

Add `--db /path/to/snapshot-copy.sqlite3` to monitor explain for saved Source0,
check and due-time context. Edit provider code via [Contributing](../CONTRIBUTING.md#where-to-edit).

## Version rules

Use native nvchecker entries. An inline entry belongs before the first table;
a multiline entry can appear after other tables. Define each track once:

```toml
python-requests = { source = "pypi", pypi = "requests" }

["widget@3"]
source = "pypi"
pypi = "widget"
include_regex = '^3\.[0-9]+\.[0-9]+$'
```

Package name defaults to track name. Compare formal releases; preserve explicit
maintenance lines. Shared KDE/Qt homepages do not establish component identity.

For a prerelease side observation, define another track and bind it without
changing the main comparison:

| `versions/nvchecker.toml` | `packages.toml` |
|---|---|
| `["widget@prerelease"]`<br>`source = "pypi"`<br>`pypi = "widget"`<br>`use_pre_release = true` | `[widget]`<br>`watch = ["widget@prerelease"]` |

Run a single-package provider check without writing the production snapshot:

```sh
PYTHONPATH=backend python -m tracker.package check python-requests \
  --config config/tracker.toml --format human --output /tmp/requests-check.json
```

`--format human` prints a short result; the default is JSON. `--output` saves the
complete report. A matching result today does not prove two rules have identical
identity or filtering semantics.

### Supplied nvchecker sources

The backend installs these [plugins](../backend/nvchecker_source/) alongside
nvchecker. Their rules require that backend, not a bare nvchecker installation.

| Source | Identity | Selection |
|---|---|---|
| `crates_index` | `cratesio = "accesskit"` | Sparse-index versions excluding yanked releases |
| `go_proxy` | `url = "…/@latest"` | Formal Go releases, excluding prereleases and pseudo-versions |
| `anitya_stable` | `anitya_id = 7306` | First provider-ordered `stable_versions` value |

`crates_index` and `anitya_stable` accept a custom `url` **instead of** their identity
field. Common nvchecker filters/normalization still apply. To filter an Anitya
maintenance line **before** taking the first result, use native `jq`; filtering
after `anitya_stable` would discard the provider's other candidates.

### Git snapshots

A complete Source0 commit must agree with the RPM `+gitDATE.shortsha` identity;
a name or prefix alone is insufficient. Confirm the branch from repository refs:

| `versions/nvchecker.toml` | `packages.toml` |
|---|---|
| `["widget@commits"]`<br>`source = "git"`<br>`git = "https://github.com/example/widget"`<br>`use_commit = true`<br>`branch = "main"` | `[widget]`<br>`compare = "widget@commits"`<br>`watch = ["widget"]` |

The old release rule stays as a watch. Different commits mean `changed`, not a
proven newer release. The list uses `YYYYMMDD.xxxxxx`; target dates come only from
provider `revision_creation_time`, so plain Git without that field shows a short
hash. Colliding prefixes are extended. Detail/API retain full identities.
Commits do not become License/Requires upgrade-version inputs.

## Other monitor identities

Registry-backed monitors reuse saved Source0 identity, then native-rule identity;
explicit settings take precedence. EOL needs a product and cycle policy:

```toml
# packages.toml
[openssl.monitors.eol]
product = "openssl"
cycle_parts = 2

[widget.monitors.license]
pypi = "upstream-widget"
```

| Identity | Reused by |
|---|---|
| PyPI | Security, License, runtime requirements, Yanked |
| crates.io | Security, License, Yanked; toolchain requirements are not runtime dependencies |
| Go module | Security, deps.dev license, proxy withdrawal; the build graph is not runtime requirements |
| CPAN distribution | License and static runtime prerequisites; module and distribution versions differ |
| Complete Source0 commit | OSV commit query; repository matches do not establish subpackage applicability |

Missing mappings remain unknown. `NotPackaged` requires a reviewed mapping absent
from a fresh inventory. PEP 508 conditions use the configured target, never the
collector host. Dynamic CPAN prerequisites and ambiguous metadata stay unsupported.
Adapter-specific fields and new backends are in [Monitor porting](../docs/monitor-porting.md).

## Add a package

OBS inventory supplies rows independently of upstream tracking. Offline `setup`
currently proposes rules only from crates.io Source0 evidence:

```sh
PYTHONPATH=backend python -m tracker.package setup PACKAGE \
  --config config/tracker.toml --db /path/to/snapshot-copy.sqlite3 \
  --output /tmp/package-proposal.json
```

Review identity and release policy before adding the entry. `entry=null` means no
candidate was generated; inspect `reason`, not just the count. Aliases belong in
identity data. A SPEC Name/directory mismatch is a packaging defect: an exception
must state its `TODO(drop)` removal condition.

Broader discovery may contact Anitya; `--verify` additionally runs nvchecker.
Neither operation changes running configuration:

```sh
PYTHONPATH=backend python -m tracker.monitors.version.discover \
  --config config/tracker.toml --db /path/to/snapshot-copy.sqlite3 \
  --output /tmp/version-candidates --verify
```

Keep exact prerelease Source0 identities for registry queries even if RPM Version
normalizes their spelling. Discovery is a proposal step, never runtime rule synthesis.

## Promote reviewed changes

Keep the original `BASE`, edited `CANDIDATE` and deployed `RUNTIME` directories.
`REVIEW` and `PREPARED` below must not exist:

```sh
PYTHONPATH=backend python -m tracker.package plan \
  --base-config "$BASE/tracker.toml" --config "$CANDIDATE/tracker.toml" \
  --runtime-config "$RUNTIME/tracker.toml" --output "$REVIEW" --format human
# Review the generated file and semantic differences before applying:
PYTHONPATH=backend python -m tracker.package apply \
  --review "$REVIEW" --runtime-config "$RUNTIME/tracker.toml" \
  --destination "$PREPARED" --format human
```

Conflicts or input drift reject promotion. It handles version rules, package
policy, BuildSystem styles and monitor settings; other operator settings, including
dependency mappings, need explicit edits in the new config directory. Apply creates
a directory, not a service switch. [Preflight and upgrade](../docs/deployment.md#upgrade-and-recovery)
that configuration without overwriting the running one.

## SPEC inputs

`[spec].extra_macro_packages` selects macro packages in the managed Git tree.
`[spec.local_sources]` maps files needed for native `%include`, such as a patch
series. Their hashes invalidate parse caches; missing inputs fail the parse.
The parser does not download Source archives or replace RPM expansion with text guessing.
