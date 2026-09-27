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
| Persist state or change HTTP contracts | `backend/tracker/state.py`, `api.py` |

Tests follow these responsibilities under `backend/tests/`. Shared fixtures belong
in `conftest.py` or `helpers/`, not in another test module. See [Design](docs/design.md)
for write ownership and trust boundaries.

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

## Write documentation and comments

Keep instructions with their task: package edits in the configuration guide,
operator commands in deployment, adapter contracts in porting, and cross-cutting
invariants in design. Link to the owner instead of copying it.

A comment should explain something the code cannot: a protocol exception, an
ownership constraint, or the reason an apparently simpler implementation is wrong.
Do not narrate control flow or repeat types and names. Use a docstring for a
caller's non-obvious contract, including failure or side effects when relevant;
use a nearby comment for an implementation constraint. Do not turn an observed
limitation into a guarantee. Check examples against fixtures, not live releases.

Reference practices: [GNU manuals](https://www.gnu.org/prep/standards/html_node/GNU-Manuals.html),
[Python docstrings](https://peps.python.org/pep-0257/),
[Rust documentation](https://doc.rust-lang.org/rustdoc/how-to-write-documentation.html),
[Java API contracts](https://www.oracle.com/java/technologies/javase/api-specifications.html).

## Request review

Submit one focused change. Review all code and text, including generated content;
briefly identify substantial tool-generated material without attaching chat logs.
Keep required lock files, fixtures and license copies. Automated agents need
human approval before publishing issues, review comments or batches of PRs.

Describe the problem and reproduction, why this fix is sufficient, checks actually
run, and anything unverified or requiring a maintainer's decision. Do not retell
the diff or report someone else's test results as your own.
