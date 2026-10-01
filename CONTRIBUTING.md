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
| Local brand assets and provenance | `frontend/src/assets/logos/catalog.json`; BuildSystem mapping in `config/distribution.toml` |
| Persist state or change HTTP contracts | `backend/tracker/state.py`, `api.py` |

Tests follow these responsibilities under `backend/tests/`. Shared fixtures belong
in `conftest.py` or `helpers/`, not in another test module. See [Design](docs/design.md)
for write ownership and trust boundaries.

SVGs are vendored at fixed upstream revisions. Add attribution, usage terms and a
checksum to the asset catalog; About reads the same catalog. Use explicit identity
mappings, not name guessing or remote icon services. Unknown icons remain text.

## Run checks

Local checks require Python 3.14+ and Node 22.13+. Native tests also require Linux,
RPM bindings, Landlock ABI 6+ and seccomp. Install the backend `test` and `quality`
extras (or the test lock), then run `npm ci` in `frontend`:

```sh
python3 scripts/check-architecture.py
python3 scripts/api-types.py --check
(cd backend && ruff check tracker nvchecker_source && pyright && lint-imports --no-cache && deptry .)
(cd backend && vulture tracker nvchecker_source --min-confidence 100 && python3 -m pytest -q)
(cd frontend && npm run quality && npm run check && npm test)
```

For the full image and real-entrypoint checks, use [Image checks](#image-checks).

After changing response models, run `python3 scripts/api-types.py` and review the
resulting `frontend/src/lib/api.generated.ts`. `openapi-typescript` compiles the
reachable UI schemas; `src/lib/document.ts` selects display primitives. Do not
hand-edit generated types or copy provider models into components.

Import Linter guards backend dependency direction. `scripts/check-architecture.py`
additionally guards transitive runtime I/O and configuration policy. Frontend
`quality` runs ESLint, Knip and dependency-cruiser; Astro checks and Knip cover
`.astro` while dependency-cruiser checks the TS/JS graph. Generated schema exports
and the built SSR entry are not unused-source candidates.

Use `radon cc backend/tracker -s -n C` to locate complex functions. Treat scores and
lower-confidence dead-code reports as investigation input, not deletion approval.

Run `(cd frontend && npm run duplicates)` for copy/paste candidates, or
`npm run duplicates:similar` in the same directory to include renamed blocks.
The root `.jscpd.json` scopes hand-written production code; tests and generated
API types are excluded. Reports do not fail on a duplication percentage. Review
whether a shared rule must change in both locations before extracting it; imports,
provider-specific policies and independent upgrade entrypoints may stay separate.
Keep release behavior and project-owned contract regressions in the suite.
Third-party tool evaluations, refactor comparisons and one-off audit experiments
belong outside the source tree, not in permanent regression tests.

Dependencies are declared in `backend/pyproject.toml`. Update the runtime and test
locks together when changing them (`uv` is a development tool):

```sh
uv pip compile backend/pyproject.toml --python-version 3.14 --python-platform linux --no-header --no-annotate -o backend/requirements.lock
uv pip compile backend/pyproject.toml --extra test --extra quality --python-version 3.14 --python-platform linux --constraint backend/requirements.lock --no-header --no-annotate -o backend/requirements-test.lock
```

Regression tests fix their inputs, not today's upstream versions or package
counts. Test shipped configuration against schema and policy. Freeze semantic
clocks; synchronize concurrent tests with events rather than elapsed sleeps.

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

The native gate runs without provider network access and rejects skipped tests.
Building its disposable test layer requires the package index; the runtime image
omits test tools. The entrypoint and release tests cover persistent mounts,
installation, migration, failed-upgrade rollback and an independent restore.

CI publishes the same tested image after these gates pass. Trusted main/tag runs
can publish; pull-request jobs cannot. Publication targets linux/amd64; additional
architectures require their own native and entrypoint checks. Package owners must
make the GHCR package Public for anonymous pulls. Target-host isolation, real
providers, HTTPS, reboot recovery and off-host restore drills require separate
operator acceptance; see [Deployment](docs/deployment.md).

## Documentation and comments

Put each instruction with its task: configuration, deployment, monitor porting
or design. Link to that owner rather than repeat it. Follow the
[GNU manual structure](https://www.gnu.org/prep/standards/html_node/GNU-Manuals.html):
introduce the task, place the runnable example beside its explanation, then state
the result and failure conditions. Keep reference details easy to locate.
Check commands, paths and links against the implementation.

Comments explain protocol exceptions, ownership or why a simpler implementation
would be wrong; they do not narrate code. Docstrings document a caller's non-obvious
contract, side effects and failures. Use synthetic examples rather than assertions
about today's provider versions or package counts.

## Request review

Submit one focused change. Review all code and text, including generated content;
briefly identify substantial tool-generated material without attaching chat logs.
Keep required lock files, fixtures and license copies. Automated agents need
human approval before publishing issues, review comments or batches of PRs.

Describe the problem and reproduction, why this fix is sufficient, checks actually
run, and anything unverified or requiring a maintainer's decision. Do not retell
the diff or report someone else's test results as your own.
