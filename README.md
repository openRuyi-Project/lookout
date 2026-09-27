# openRuyi Package Monitor

A read-only view of [openRuyi](https://openruyi.cn) packages: source versions,
upstream releases, OBS builds and upstream maintenance evidence.

Collectors save observations in SQLite. FastAPI and Astro serve that snapshot;
page requests do not contact providers. Missing, failed and stale checks remain
visible. A security match concerns the queried upstream component, not the
exploitability of the distributed RPM; local patches and bundled dependencies
are not evaluated.

| I want to… | Read |
|---|---|
| Run, upgrade or roll back the service | [Deployment](docs/deployment.md) |
| Add a package or correct its monitor configuration | [Configuration](config/README.md) |
| Change code and run tests | [Contributing](CONTRIBUTING.md) |
| Add a monitor | [Monitor porting](docs/monitor-porting.md) |
| Understand ownership, consistency and security limits | [Design](docs/design.md) |

Deployment requires a Linux container, native RPM bindings and Landlock ABI 6+
for confined SPEC parsing. Configuration and persistent data are separate mounts.

## API

The website exposes `/api/v2/packages`, `/api/v2/status` and `/api/v2/export`.
`/openapi.json` describes the response contracts. Package pages link observations
to their sources.

## License

[MulanPSL-2.0](LICENSE).
