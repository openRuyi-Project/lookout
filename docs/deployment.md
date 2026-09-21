# Deployment

This is the canonical deployment procedure for the single openRuyi-monitor container. Astro SSR, FastAPI, and the configurable `obs`, `upstreams`, `specs`, and `monitors` tasks run in one container. The web/API process does not wait for a complete SPEC clone; collection state is reported separately.

## Prerequisites

Use a dedicated non-root Linux account, cgroup v2, a local persistent filesystem, Landlock ABI 6 or newer, seccomp, Python 3.11+ for host tools, and rootless Podman with Quadlet. Verify the actual host with the image checks; do not infer support from a kernel version string. The runtime image defaults to UID/GID `10001` and the data directory must be writable through the rootless `keep-id` mapping.

## Build and test one immutable image

Build and deploy the same tested image; do not rebuild from a moving branch after these checks:

```sh
IMAGE="localhost/openruyi-monitor:$(git rev-parse --short HEAD)"
podman build --format docker -t "$IMAGE" -f Containerfile .
CONTAINER_ENGINE=podman deploy/check-image.sh "$IMAGE"
CONTAINER_ENGINE=podman python3 deploy/smoke-image.py "$IMAGE"
```

Docker may be used for CI with `docker build`; the smoke command still uses the image's real entrypoint. These checks do not prove provider reachability, SELinux policy, HTTPS, or reboot recovery on a target host.

## Configuration and preflight

Create configuration outside the source checkout. The initializer refuses to overwrite an existing destination:

```sh
python3 deploy/init-config.py /srv/openruyi-monitor/config
install -d -m 0750 /srv/openruyi-monitor/data /srv/openruyi-monitor/backups
```

Keep `tracker.toml` and `versions/nvchecker.toml` read-only in the container; keep data and backups in separate persistent directories. Before installing a service, run preflight with the same image, user mapping, mounts, and security options that production will use. It does not contact upstreams or create a database:

```sh
podman run --rm --network none --read-only --cap-drop=all \
  --security-opt=no-new-privileges --userns=keep-id:uid=10001,gid=10001 \
  -v /srv/openruyi-monitor/config:/config:ro,Z \
  -v /srv/openruyi-monitor/data:/data:Z \
  "$IMAGE" python -m tracker.runtime_checks \
    --config /config/tracker.toml --db /data/state/tracker.sqlite3
```

## Rootless Quadlet

Render `deploy/quadlet/openruyi-monitor.container.in` by replacing exactly `@IMAGE@`, `@CONFIG_DIR@`, and `@DATA_DIR@` with absolute paths and the tested image tag. Refuse to overwrite an existing unit and verify no placeholder remains. Run the user generator in dry-run mode, then have the operator copy the rendered unit to `~/.config/containers/systemd/openruyi-monitor.container`:

```sh
QUADLET_UNIT_DIRS="$TMP_QUADLET_DIR" \
  /usr/lib/systemd/system-generators/podman-system-generator --user --dryrun
systemctl --user daemon-reload
systemctl --user start openruyi-monitor.service
journalctl --user -u openruyi-monitor.service -f
```

Do not run ordinary `systemctl enable` on the generated service. Configure linger and the user service directory as an operator/administrator task. Do not run a second full collector instance or a host collection timer against the same SQLite database.

The unit binds only `127.0.0.1:18730`. For HTTPS, use the host Caddy example in [`deploy/Caddyfile.example`](../deploy/Caddyfile.example), replace the domain, and configure DNS and access control on the host. Port 18731 is not published.

## Health acceptance

`/livez` only proves the Node-to-FastAPI liveness path. `/readyz` and the compatibility `/healthz` report whether a readable snapshot exists. The status API and logs report freshness, coverage, failures, and degraded collection. An empty data directory may be live but not ready; degraded must not be reported as fully healthy.

The optional cve-bin-tool integration is unavailable until its controlled scanner and fresh database are installed under the configured `/data/cve` location. Do not turn unavailable into “no vulnerabilities”; other OSV-based evidence remains independent.

## Backup

Never copy a live SQLite file with `cp`. Use the atomic SQLite Backup API command and store the result independently:

```sh
python3 deploy/backup-snapshot.py \
  --db /srv/openruyi-monitor/data/state/tracker.sqlite3 \
  --output /srv/openruyi-monitor/backups/tracker-YYYYMMDDTHHMMSSZ.sqlite3 \
  --timeout-seconds 30
```

Back up the matching configuration and image identifier as well. Arrange at least one independent copy and periodically restore a backup into a separate test directory.

## Upgrade and rollback

Record the image tag, configuration directory, data directory, and backup path. Build and test the new image, run preflight, take a backup, stop the old service, and switch the unit to the new image while retaining the data directory. For a code rollback, restore the previous image and matching configuration; do not delete or automatically restore the database. A data restore is a separate explicit operation, and schema compatibility must be checked before using an older image.

The operator must finally verify real provider data progression, HTTPS, continued operation after logout, host reboot recovery, and a backup restore on the target host. These host and production-data checks are not performed by the repository test suite or by this deployment procedure.
