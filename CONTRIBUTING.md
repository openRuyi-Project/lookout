# Contributing

For a package correction, use the [configuration guide](config/README.md).
For a new monitor, use the [porting guide](docs/monitor-porting.md).
For data ownership and safety boundaries, use the [maintainer reference](docs/design.md).

## Development checks

```sh
python3 scripts/check-architecture.py
# In the native Linux environment described below:
python3 scripts/api-types.py --check
cd backend && python3 -m pytest -q
cd ../frontend && npm ci && npm run check && npm test
```

After changing response models, regenerate `frontend/src/lib/api.generated.ts`
with `python3 scripts/api-types.py`, then review the diff. Keep lock files, fixtures
and license copies needed for reproducible builds and distribution.

The CI workflow separates lightweight checks from the container/native suite.
Native tests require RPM, Landlock ABI 6+, seccomp and an unprivileged worker.
`deploy/check-image.sh IMAGE` runs the suite in the built image without provider
network access. An unsupported kernel fails the check; skipped tests fail this
release gate. Its temporary filesystem permits executable installer-test shims;
production mount policy and SPEC confinement are unchanged. Live provider validation is a separate, explicitly requested check.

## Before requesting review

- Own and review every submitted change, including generated code and text.
- Address one clear problem; leave unrelated refactors and documentation expansion out.
- Give reproducible evidence, actual checks and unverified scope. Do not present
  existing test results as tests you ran.
- Briefly identify substantial generated content; do not commit chat transcripts.
- Automated agents need human approval before publishing issues, review comments
  or batches of pull requests.

A review request should answer these questions without retelling the diff:

```text
Problem and reproduction:
Why this change:
Checks actually run:
Compatibility or unverified scope:
Decisions needed from the maintainer:
```
