# Deployment

One container runs Astro SSR, FastAPI and independently scheduled OBS, version,
SPEC and monitor collection. SQLite, the managed SPEC clone and provider caches
live on persistent local storage. Only one complete instance may use that storage.
Neither a page request nor an image upgrade starts a full recollection.

## Requirements

Use a Linux host with cgroup v2, Landlock ABI 6+ and seccomp. Run the native image
tests on that host; the kernel version alone is not sufficient evidence. Host
release tools require Python 3.11+. Docker Engine or Rootless Podman with Quadlet
is required. The application image supplies Python 3.14 and Node.

Use a dedicated deployment account, local disk and private backup directories.
Docker daemon access is privileged; use rootless Docker where supported or limit
membership of its administrator group. Do not expose the engine socket to the app.
Do not place this SQLite directory on a network filesystem.

## Install a release with Docker

Obtain the release for the host architecture from the trusted project release
channel. Keep its directory: it contains the tested image, installation and upgrade
tools, manifest and checksums. Checksums detect corruption, not a substituted
publication channel. From that directory:

```sh
sha256sum --check SHA256SUMS
python3 install.py --name openruyi-monitor --port 18730
```

The installer validates the image identity, creates three explicitly named volumes
(`openruyi-monitor-config`, `-data`, `-backups`), checks the runtime and starts the
real entrypoint. It refuses existing containers or volumes. The application runs
as UID/GID 10001 with a read-only root and config, dropped capabilities and bounded
resources/logs. Only the one-time initialization helper owns new empty volumes as
root. No anonymous data volume, production chown or permissive host directory is
needed. Failed installation retains its volumes for inspection; do not rerun with
a different name merely to bypass a failure.

To supply reviewed configuration at installation:

```sh
python3 install.py --name openruyi-monitor --port 28730 \
  --config /absolute/private/config --memory 8g --cpus 4 --pids-limit 512
```

Configuration files are read as the operator and copied into the private config
volume. Private host files need not be made readable to UID 10001. Symlinks are
rejected. An omitted `--config` initializes the release's defaults only on first
installation; upgrading never reinitializes config or data.

## Administrator settings

| Setting | Docker | Rootless Quadlet | Default / effect |
| --- | --- | --- | --- |
| Host HTTP port | `install.py --port PORT`; `maintain.py --port PORT` to change | `PublishPort=127.0.0.1:PORT:8080` | 18730; recreate service, no frontend build |
| Memory | `--memory 8g`; `docker update --memory 8g NAME` | `Memory=8g` | 8 GiB ceiling, not a measured minimum |
| CPU | `--cpus 4`; `docker update --cpus 4 NAME` | `[Service] CPUQuota=400%` | four CPUs maximum |
| Processes | `--pids-limit 512`; `docker update --pids-limit 512 NAME` | `PidsLimit=512` | includes collector child processes |
| Outbound proxy | `--env TRACKER_MONITOR_PROXY=URL` | `Environment=TRACKER_MONITOR_PROXY=URL` | absent; configure Git's proxy separately if needed |
| SPEC clone | `--env TRACKER_SPEC_REPO=/data/spec-full.git` | `Environment=TRACKER_SPEC_REPO=/data/spec-full.git` | overrides `[spec].repo`; an empty value falls back to TOML |
| SPEC source | `tracker.toml`: `[spec].url`, `[spec].branch` | same | required for a managed clone; no source is inferred by the executable |
| OBS project and targets | mounted `tracker.toml`: `[obs]`, `[[targets]]` | same | release defaults |
| Collection budget | `tracker.toml`: `[collector]`, `[monitors]` | same | existing intervals/workers/timeouts; restart after editing |
| Package identities and rules | `packages.toml`, `versions/nvchecker.toml` | same | see [configuration](../config/README.md) |
| Provider credentials | private keyfile referenced by native nvchecker config | same | never a frontend `PUBLIC_*` variable |
| Backup location and age | `maintain.py --output PATH` / `--status DIR` | same, with `--unit` | separate host directory; age budget 26 h |

The **host** port is configurable; container ports remain web 8080 and private API
18731. Runtime environment goes to server processes, not browser assets. Domain,
TLS, authentication and public/private ingress belong to an independent host
proxy. The project neither discovers domains nor provisions certificates. Publish
only `127.0.0.1`; do not use host networking or publish the API. A host proxy can
forward to that loopback endpoint; another container's localhost is not this host.
The [Caddy example](../deploy/Caddyfile.example) is optional external configuration.

Docker resource updates persist through upgrades. For config edits, export the
private config, review it with the [existing configuration tools](../config/README.md),
then import it as a **new config volume**:

```sh
umask 077
mkdir /absolute/private/config-next
docker cp openruyi-monitor:/config/. /absolute/private/config-next/
# Edit and validate config-next, then apply it without replacing data:
python3 maintain.py --container openruyi-monitor --config /absolute/private/config-next
```

This operation retains the previous config volume, preflights the candidate and
recreates the service. It can be combined with `--port`. On startup failure it
returns to the previous container/config. Secret values should be kept in private
files, not command history. No `.env` file is loaded implicitly; installed Docker
environment overrides and Quadlet `Environment=` lines are preserved on upgrade.

## Rootless Podman / Quadlet

Use a dedicated non-root account and enable linger through the host administrator.
The host upgrade tool uses systemd's `busctl` to request service transitions and
reloads, then checks service state and HTTP readiness.
Keep config, data and backups outside the checkout. With the release loaded, copy
initial config from the image's `/app/config` into a **new** private directory;
source checkouts also provide `deploy/init-config.py`, which refuses overwriting.

Render `openruyi-monitor.container.in`: replace `@IMAGE@` with the tested image ID,
`@CONFIG_DIR@` and `@DATA_DIR@` with absolute existing directories. Paths must not
contain whitespace, colons, quotes or systemd `%` specifiers. Edit `PublishPort`
for a different loopback port and the resource fields as required. There are no
domain or TLS placeholders. Use one unquoted `KEY=value` per `Environment=` line.

Validate in a temporary directory before installing the unit:

```sh
QUADLET_UNIT_DIRS="$TMP_QUADLET_DIR" \
  /usr/lib/systemd/system-generators/podman-system-generator --user --dryrun
# Operator: install the reviewed file, without overwriting an existing unit.
systemctl --user daemon-reload
systemctl --user start openruyi-monitor.service
journalctl --user -u openruyi-monitor.service
```

Use the actual generator location on the host. Missing generator is not a passed
check. The template maps the operator to container 10001:10001; do not use
`chmod 777`. `:Z` is a private SELinux label: stop the service before another
container mounts that directory for preflight/migration. Online backup instead
executes inside the existing container. Quadlet `[Install]` handles activation;
do not `systemctl enable` the generated service. Do not retain a second legacy
container or host collection timer.

## Health and operational checks

- `/livez`: Node → FastAPI request chain.
- `/readyz` (also `/healthz`): readable snapshot, possibly `degraded`.
- `/api/v2/status`: collection coverage and timestamp progression.

Readiness does not claim all providers are reachable. Freshness and coverage must
be observed separately. An empty installation can be live before ready. Failed
checks retain dated evidence rather than implying there are no issues.

The optional `/opt/cve` scanner is not included. Vendor/product checks require its
controlled installation and a fresh `/data/cve` database; unavailable coverage is
not “no vulnerabilities.” OSV checks are independent.

## Online backups

```sh
install -d -m 0700 /absolute/backups
python3 maintain.py --container openruyi-monitor \
  --output "/absolute/backups/tracker-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
python3 maintain.py --container openruyi-monitor --status /absolute/backups
```

For Podman, replace `--container NAME` with `--unit /absolute/path/NAME.container`.
Use a host scheduler to run **backup**, not collection, daily. A nonzero status
means missing/old backup or insufficient space; arrange notification through the
host's existing monitoring. Docker logs rotate at 10 MiB × 3; Podman uses host
journald retention and unit rate limits. Both need disk monitoring.

Backup uses SQLite's online Backup API, verifies the snapshot and publishes an
exclusive 0600 file. `cp` of a live database and API export are not backups. Keep
config/credentials and image identity separately, plus at least one independent
storage copy. Retention/deletion is an operator policy, never an upgrade side effect.

## Upgrade and recovery

From a verified new release directory, review and apply:

```sh
python3 upgrade.py --container openruyi-monitor --backups /absolute/backups
python3 upgrade.py --container openruyi-monitor --backups /absolute/backups --apply
# Podman: use --unit /absolute/path/openruyi-monitor.container instead.
```

The transaction pins the image, stops the sole writer, recovers any hot journal,
backs up and performs a supported migration, then preflights and starts the new
image. It preserves config/data identities, port, resources and environment.
The image and its labels are checked against the manifest before stopping service.
A short interruption is expected. Existing evidence/caches remain; unchanged
fingerprints do not become new queries merely because the image changed.

Each transaction records the previous runtime, image, backup and result in a new
private directory. The old image and volumes remain. On failure the old image is
started only if it can read the current database. Otherwise the service stays
stopped: no empty database, journal deletion or automatic rewind. After an
interruption, inspect that directory and the engine before retrying.

For code rollback, stop all writers, preflight the old image against current data,
then restore its saved container/unit and matching configuration. Unknown storage
formats are rejected. A schema migration may require data restoration instead:

1. Restore the verified backup into an **independent** data directory/volume.
2. Run the matching image against that copy; check integrity, package versions,
   monitor evidence and HTTP readiness.
3. Explicitly choose whether to switch to the restored copy. Retain the current
   data too: observations after the backup would otherwise be lost.

The first row-storage release migrates legacy snapshots once. Later compatible
upgrades do not rewrite the dataset. Do not run two full instances against one
SQLite directory, even during testing or rollback.

Before upgrading a legacy config that enabled a managed SPEC clone without
`[spec].url` and `branch`, record its existing Git origin and branch in those
fields. Preflight rejects missing identity rather than selecting another source.

## Build and release checks

From a clean explicit commit, run on the target architecture:

```sh
VERSION=$(python3 -c 'import tomllib; print(tomllib.load(open("backend/pyproject.toml", "rb"))["project"]["version"])')
IMAGE="openruyi-monitor:$(git rev-parse --short=12 HEAD)"
docker build --build-arg SOURCE_REVISION="$(git rev-parse HEAD)" \
  --build-arg RELEASE_VERSION="$VERSION" --build-arg SOURCE_URL="${SOURCE_URL:-}" -t "$IMAGE" -f Containerfile .
CONTAINER_ENGINE=docker deploy/check-image.sh "$IMAGE"
CONTAINER_ENGINE=docker python3 deploy/smoke-image.py "$IMAGE"
python3 deploy/smoke-release.py "$IMAGE"
CONTAINER_ENGINE=docker python3 deploy/release.py --image "$IMAGE" --output /new/release-dir
```

Podman builds use `--format docker` to preserve HEALTHCHECK and
`CONTAINER_ENGINE=podman` for image/native gates. `smoke-release.py` specifically
exercises Docker installation/upgrade; Quadlet additionally needs generator and
isolated systemd service tests. The packager rejects dirty source or mismatching
revision/version. Scan the saved image with a current vulnerability database;
record findings and database age rather than treating build success as a scan.

`SOURCE_URL` is optional source-provenance metadata supplied by the publisher;
CI uses its repository context. It is not the deployed website URL and is never
read by the service. The monitored SPEC repository is a separate `[spec]` setting.

Deploy the exact tested artifact, not a rebuild of moving `main`. Keep immutable
release versions. Target-host acceptance still includes native isolation, SELinux,
actual provider timestamps, operation after logout/reboot, backup recovery and
whatever external access the operator configured. Offline gates do not prove those.
