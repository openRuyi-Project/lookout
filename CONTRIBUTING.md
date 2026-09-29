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
| Local brand assets and provenance | `frontend/src/assets/logos/catalog.json`; BuildSystem mapping in `config/tracker.toml` |
| Persist state or change HTTP contracts | `backend/tracker/state.py`, `api.py` |

Tests follow these responsibilities under `backend/tests/`. Shared fixtures belong
in `conftest.py` or `helpers/`, not in another test module. See [Design](docs/design.md)
for write ownership and trust boundaries.

SVGs are vendored at fixed upstream revisions. Add attribution, usage terms and a
checksum to the asset catalog; About reads the same catalog. Use explicit identity
mappings, not name guessing or remote icon services. Unknown icons remain text.

## Run checks

Use Python 3.14+. In a Linux environment with the backend dependencies, native
RPM bindings, Landlock ABI 6+ and seccomp:

```sh
python3 scripts/check-architecture.py
python3 scripts/api-types.py --check
(cd backend && python3 -m pytest -q)
(cd frontend && npm ci && npm run check && npm test)
```

For the full image and real-entrypoint checks, use the
[deployment build procedure](docs/deployment.md#build-and-test). The native gate
runs without provider network access and rejects skipped tests. Building its
disposable test layer requires the package index; the shipped image omits test
tools. Live-provider checks are separate from this gate.

After changing response models, run `python3 scripts/api-types.py` and review the
resulting `frontend/src/lib/api.generated.ts`. Do not hand-edit generated types.

Dependencies are declared in `backend/pyproject.toml`. Update the runtime and test
locks together when changing them (`uv` is a development tool):

```sh
uv pip compile backend/pyproject.toml --python-version 3.14 --python-platform linux --no-header --no-annotate -o backend/requirements.lock
uv pip compile backend/pyproject.toml --extra test --python-version 3.14 --python-platform linux --constraint backend/requirements.lock --no-header --no-annotate -o backend/requirements-test.lock
```

Regression tests fix their inputs, not today's upstream versions or package
counts. Test shipped configuration against schema and policy. Freeze semantic
clocks; synchronize concurrent tests with events rather than elapsed sleeps.

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
