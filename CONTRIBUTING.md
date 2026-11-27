# Contributing

For package data, start with [Configuration](config/README.md). For an additional
monitor, follow [Monitor porting](docs/monitor-porting.md).

## Where to edit

| Change | Entry point |
|---|---|
| Package version rules, comparison policy or monitor identities | [Configuration](config/README.md) |
| Collect a monitor's observations | `backend/tracker/monitors/<name>/` or `<name>.py` |
| Register an evidence adapter | `backend/tracker/monitors/registry.py` |
| Schedule checks and publish their results | `backend/tracker/monitors/runner.py`, `schedule.py` |
| Access external registry data | `backend/tracker/providers/` |
| Derive package state, filters and counts | `backend/tracker/readmodel/` |
| Select and group visible facts | `backend/tracker/presentation/` |
| Render documents and controls | `frontend/src/components/document/`, `pages/`, `styles/` |
| Local brand assets and provenance | `frontend/src/assets/logos/catalog.json`, with BuildSystem mappings in `config/distribution.toml` |
| Persist state or change HTTP contracts | `backend/tracker/state.py`, `api.py` |

Tests follow these responsibilities under `backend/tests/`. Shared fixtures belong
in `conftest.py` or `helpers/`, not in another test module. See [Design](docs/design.md)
for write ownership and trust boundaries.

For artwork provenance and usage terms, see [Interface artwork](docs/assets.md).
Use explicit identity mappings, not name guessing or remote icon services. Unknown icons remain text.

## Run checks

Local checks require Python 3.14+ and a Node version matching
[`frontend/package.json`](frontend/package.json) `engines`. `npm ci` rejects
unsupported versions. Native tests also require Linux,
RPM bindings, Landlock ABI 6+ and seccomp. Install the backend `test` and `quality`
extras (or the test lock), then run `npm ci` in `frontend`:

```sh
python3 scripts/check-architecture.py
python3 scripts/api-types.py --check
(cd backend && ruff check tracker nvchecker_source && pyright && lint-imports --no-cache && deptry .)
(cd backend && vulture tracker nvchecker_source --min-confidence 100 && python3 -m pytest -q)
(cd frontend && npm run quality && npm run check && npm test)
```

For local coverage, run `(cd backend && pytest --cov --cov-report=term)`.
Add `--cov-report=xml` to write `backend/coverage.xml`.

Then run [Image checks](#image-checks).

### Image checks

Build and test one image, not a running production instance:

```sh
IMAGE=openruyi-lookout:test
VERSION=$(python3 -c 'import tomllib; print(tomllib.load(open("backend/pyproject.toml", "rb"))["project"]["version"])')
docker build --build-arg SOURCE_REVISION="$(git rev-parse HEAD)" \
  --build-arg RELEASE_VERSION="$VERSION" -t "$IMAGE" -f Containerfile .
CONTAINER_ENGINE=docker deploy/check-image.sh "$IMAGE"
CONTAINER_ENGINE=docker python3 deploy/smoke-image.py "$IMAGE"
python3 deploy/smoke-release.py "$IMAGE"
```

The container build pins the official Node image by digest and copies the same
Node/npm into the Fedora runtime. The test image inherits them. Update that pin
in `Containerfile` when upgrading Node.

Set `COVERAGE_OUTPUT_DIR` to a host directory before calling `check-image.sh` to
collect `backend-coverage.xml`, a Cobertura report with repository-relative file paths. The script exports it
from the read-only, offline container without a writable host mount. CI enables
coverage. Leave the variable unset for a run without coverage.

CI reuses build layers, but runs tests on every code change. Only the document
paths listed in `scripts/ci-scope.py` skip image checks. Release always runs them.
Use `CI result` as the required branch check. PRs read shared caches. Only trusted
`main` runs write them. Weekly builds refresh system dependency layers.

The native gate runs without provider network access and rejects skipped tests.
Building its disposable test layer requires the package index. The runtime image
omits test tools. The entrypoint and release tests cover persistent mounts,
installation, migration, failed-upgrade rollback and an independent restore.

Target-host isolation, real providers, HTTPS, reboot recovery and off-host restore
require separate [operator acceptance](docs/deployment.md#acceptance-and-cleanup).

## Checks by change

### API contracts

After changing response models, run `python3 scripts/api-types.py` and review the
resulting `frontend/src/lib/api.generated.ts`. `openapi-typescript` compiles the
reachable UI schemas. `src/lib/document.ts` selects display primitives. Do not
hand-edit generated types or copy provider models into components.

Keep generated schema exports and the built SSR entry out of dead-code removal.

### Complex or repeated code

Use `radon cc backend/tracker -s -n C` to locate complex functions. Treat scores and
lower-confidence dead-code reports as investigation input, not deletion approval.

Run `(cd frontend && npm run duplicates)` for copy/paste candidates, or
`npm run duplicates:similar` in the same directory to include renamed blocks.
The root `.jscpd.json` selects hand-written production code. It excludes tests and generated API types. Duplication percentages are advisory. Extract shared rules only when both copies must change together.
Imports, provider policies and independent upgrade entrypoints may stay separate.
Keep release behavior and project-owned contract regressions in the suite.
Third-party tool evaluations, refactor comparisons and one-off audit experiments
belong outside the source tree, not in permanent regression tests.

### Regression tests

Regression tests fix their inputs, not today's upstream versions or package
counts. Test shipped configuration against schema and policy. Freeze semantic
clocks. Synchronize concurrent tests with events rather than elapsed sleeps.

## Dependency updates

Use the newest stable versions compatible with the tools that consume them.
Update the controlling tools first, then select dependencies within their combined
supported ranges. Update unrelated dependencies independently.

For the frontend, Astro and its Node adapter must work together. TypeScript must
satisfy `@astrojs/check`, TypeScript ESLint and `openapi-typescript`. Use declared peer dependencies
and runtime requirements as constraints. CI must also pass.

Keep compatibility ranges in manifests and resolved versions in lock files.
Review these ranges when a controlling tool gains support for a newer dependency.
Do not bypass conflicts with `--force`, `--legacy-peer-deps`, or disabled checks.

[Dependabot](.github/dependabot.yml) proposes npm and GitHub Actions updates.
Groups batch changes, not compatibility decisions. Review related updates together.
Leave incompatible updates unmerged until their consumers support them.
Do not block all major updates to avoid one conflict.
For security fixes, upgrade the affected toolchain or use a reviewed mitigation.

Declare dependencies in `backend/pyproject.toml`. Update the runtime and test
locks together when changing them (`uv` is a development tool):

```sh
uv pip compile backend/pyproject.toml --python-version 3.14 --python-platform linux --no-header --no-annotate -o backend/requirements.lock
uv pip compile backend/pyproject.toml --extra test --extra quality --python-version 3.14 --python-platform linux --constraint backend/requirements.lock --no-header --no-annotate -o backend/requirements-test.lock
```

The frontend pins `http-cache-semantics` with an install/build-time security patch for
GHSA-ch52-4w7c-c8xp. Its source checksum rejects unreviewed upgrades. Remove the
patch only when `frontend/tests/http-cache.cjs` passes against unmodified upstream.

## Documentation and comments

Write for a reader with a task. Lead with the model or procedure they need.
Keep field details and optional paths available for lookup. Cut prose that repeats
an example. Keep each instruction in one task guide and link to it elsewhere.

Place runnable examples beside their explanations. State expected results and
failure handling. Check commands, paths and links against the implementation.
Use one term per concept. Put conditions before actions and required actions in
steps, not notes. Keep names, syntax and safety conditions exact.

Comments explain protocol exceptions, ownership or why a simpler implementation
would be wrong. Do not narrate code. Docstrings document a caller's non-obvious
contract, side effects and failures. Use synthetic examples rather than assertions
about today's provider versions or package counts.

## Request review

Submit one focused change. Review all code and text, including generated content.
Briefly identify substantial tool-generated material without attaching chat logs.
Keep required lock files, fixtures and license copies. Automated agents need
human approval before publishing issues, review comments or batches of PRs.

Describe the problem and reproduction, why this fix is sufficient, checks actually
run, and anything unverified or requiring a maintainer's decision. Do not retell
the diff or report someone else's test results as your own.

## Publish a release

In GitHub Actions, select **Release → Run workflow**, choose `main`, and select
`patch` (default), `minor`, or `major`. Use `patch` for compatible fixes,
`minor` for features, and `major` for a new major version.
The workflow increments the highest version from the project declaration and
stable release tags. It shows the selected version in the publication job title and run summary.
The workflow pins the selected commit, runs the complete image checks, and
publishes that same image as `v<VERSION>` and `sha-<commit>`. The container's OCI
version label records the release version. A commit can be released only once.

The workflow creates the tag and release. Do not create them in advance. Notes
list commits since the previous stable tag, grouped by type, with commit links
and a full diff. `cliff.toml` controls the format.

Ordinary pushes and PRs run CI only. Production consumers follow successful
Release runs. Only releases advance the `main` and `latest` image tags.
Configuration and data remain on the host.

Published images target linux/amd64. Each additional architecture needs native
and entrypoint checks. Package owners must make GHCR packages Public for
anonymous pulls.

A failed run does not authorize an automatic upgrade. Inspect its logs and any
draft release before retrying. Do not move a published version tag or reuse its
version. Use a new version and commit for corrections.
