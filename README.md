# openRuyi Package Monitor

A read-only package maintenance view for [openRuyi](https://openruyi.cn):

- **Version:** packaged RPM version against the configured upstream release line.
- **Maintenance:** actionable EOL, security-review and upgrade license findings.
- **Build:** OBS results and last successful versions for each target architecture.

OBS, the packaging Git repository and upstream providers supply observations.
The collector saves an atomic SQLite snapshot; FastAPI and Astro render it without
network collection in page requests. Failed or stale observations remain visible.
Security matches require review of local patches and build options; they do not
prove that an RPM is exploitable. ABI checking and bundled-dependency scanning are
not implemented.

## Start here

| Task | Guide |
|---|---|
| Run, upgrade or roll back the service | [Deployment](docs/deployment.md) |
| Find, add or correct a package rule | [Configuration](config/README.md) |
| Develop and submit a change | [Contributing](CONTRIBUTING.md) |
| Change collection boundaries or add a monitor | [Maintainer reference](docs/design.md) |

The supported deployment is a Linux container with native RPM bindings and
Landlock ABI 6+ for confined SPEC parsing. Configuration and data are mounted
separately from the image. The deployment guide points to the tested initializer.

Package rules live in the native `config/versions/nvchecker.toml`; comparison and
monitor policy live in `config/tracker.toml`. Use the configuration guide to locate,
check and promote one package without reading the complete rule inventory.

## Interfaces

The running service exposes uniform monitor results at `/api/v2/packages` (with v1 compatibility), `/api/v1/status`,
`/api/v1/export` and `/openapi.json`. The package list links to per-package source,
build and maintenance evidence. Network access and credentials are operator-owned.

## License

[MulanPSL-2.0](LICENSE). Required license copies and generated API types are kept
in the repository; they are not independently maintained sources of behavior.
