# Deployment

## Prerequisites

Use a dedicated non-root Linux account, cgroup v2, Rootless Podman with Quadlet,
Python 3.14+ for host tools, and local persistent storage. Native SPEC parsing
requires Landlock ABI 6+ and seccomp. Run the native check on the target host;
a kernel version string alone does not prove the required isolation works.

One container runs Astro SSR, FastAPI and independently scheduled collection for
OBS, upstream versions, SPECs and enabled monitors. Web startup does not wait for
Git cloning, and new observations do not require rebuilding the frontend.
Catatonit reaps child processes; the Python supervisor schedules collection and
handles shutdown. Do not install additional collection timers or run another
complete collector against the same database.

For consistency, caching and failure semantics, see the
[design reference](design.md). Collection intervals belong to the mounted
configuration and each monitor's refresh policy; changing the image does not
replace operator settings.

## Build and test

From a clean checkout at an explicit commit:

```sh
set -eu
test -z "$(git status --porcelain)"
IMAGE="localhost/openruyi-monitor:$(git rev-parse --short=12 HEAD)"
podman build --format docker -t "$IMAGE" -f Containerfile .
CONTAINER_ENGINE=podman deploy/check-image.sh "$IMAGE"
CONTAINER_ENGINE=podman python3 deploy/smoke-image.py "$IMAGE"
```

Deploy this same tested image, not another build from a moving branch.
`--format docker` preserves the image HEALTHCHECK. Docker CI uses `docker build`
without that flag and `CONTAINER_ENGINE=docker`. Both gates fail on unsupported
native environments; skipped native tests do not count as success.

The image defaults to UID/GID `10001:10001`. Rootless Podman maps the deploying
account to this identity with `--userns=keep-id:uid=10001,gid=10001`. Docker bind
mounts need compatible ownership; replacing the command name is not sufficient.

## Prepare configuration and storage

Keep configuration, data, and backups outside the checkout, owned by the
non-root deploying account. For example:

```sh
BASE="$HOME/.local/share/openruyi-monitor"
CONFIG_DIR="$BASE/config-$(git rev-parse --short=12 HEAD)"
DATA_DIR="$BASE/data"
BACKUP_DIR="$BASE/backups"
install -d -m 0700 "$BASE" "$DATA_DIR" "$BACKUP_DIR"
python3 deploy/init-config.py "$CONFIG_DIR"
```

The initializer refuses an existing destination. Review the external config
before starting; upgrades use [configuration promotion](../config/README.md)
into a new directory, not a copy of repository defaults over operator settings.
Do not solve permissions with `chmod 777`. Use dedicated local data directories,
not cross-host shared SQLite storage. `:Z` assigns private SELinux labels.

## Preflight without starting services

Use a new data directory or stop the existing service first. This command uses
[Podman private labels](https://docs.podman.io/en/latest/markdown/podman-run.1.html#volume-v-source-volume-host-dir-container-dir-options):
`:Z` relabels the mounted directory for this container, so it must not be run
against a directory still used by another container.

Override the image entrypoint explicitly. Passing `python ...` after the image
without this override does **not** run an independent preflight.

```sh
podman run --rm --network none --read-only --cap-drop=all \
  --security-opt=no-new-privileges --userns=keep-id:uid=10001,gid=10001 \
  --tmpfs /tmp:rw,nosuid,nodev,size=128m,mode=1777 \
  -v "$CONFIG_DIR:/config:ro,Z" -v "$DATA_DIR:/data:Z" \
  --workdir /app/backend --entrypoint /opt/venv/bin/python "$IMAGE" \
  -m tracker.runtime_checks --config /config/tracker.toml \
  --db /data/state/tracker.sqlite3
```

Preflight checks configuration, writes and removes temporary permission probes,
reads an existing database, and probes the native worker when SPEC features are
enabled. It neither contacts upstreams nor creates a database. Failures exit 2.

The service startup additionally allows SQLite to recover a hot rollback journal
under the writer lock. Standalone preflight does not perform this recovery. A
corrupt database fails startup; do not delete a journal or substitute an empty
database. Preflight success does not establish provider network availability.

## Configure the service

Use the tested image and absolute config/data directories. This renderer creates
a new temporary file exclusively and rejects unresolved placeholders:

```sh
TMP_QUADLET_DIR=$(mktemp -d)
python3 - "$IMAGE" "$CONFIG_DIR" "$DATA_DIR" "$TMP_QUADLET_DIR" <<'PY'
from pathlib import Path
import sys
image, config, data, output = sys.argv[1:]
if any(not value or any(c.isspace() or c in "\\%\"'\x00" for c in value)
       for value in (image, config, data)):
    raise SystemExit('Use values without whitespace, quotes, backslashes or % specifiers')
if image.endswith(':latest') or not (':' in image.rsplit('/', 1)[-1] or '@sha256:' in image):
    raise SystemExit('Use an explicit image tag or digest, not latest')
if not all(Path(p).is_absolute() and ':' not in p for p in (config, data)):
    raise SystemExit('Use absolute volume paths without colons')
text = Path('deploy/quadlet/openruyi-monitor.container.in').read_text()
for key, value in [('IMAGE', image), ('CONFIG_DIR', config), ('DATA_DIR', data)]:
    text = text.replace('@' + key + '@', value)
if any('@' + key + '@' in text for key in ('IMAGE', 'CONFIG_DIR', 'DATA_DIR')):
    raise SystemExit('Unresolved template placeholder')
with (Path(output) / 'openruyi-monitor.container').open('x') as stream:
    stream.write(text)
PY
QUADLET_UNIT_DIRS="$TMP_QUADLET_DIR" \
  /usr/lib/systemd/system-generators/podman-system-generator --user --dryrun
```

Use the system's actual generator path if different. Review the generated
ExecStart for UID mapping, dropped capabilities, read-only root/config, data
mount, and loopback port. Missing generator means this step is unverified, not
passed. Dry-run does not start a service.

For first installation, the operator installs the unit without overwriting one:

```sh
install -d -m 0700 "$HOME/.config/containers/systemd"
python3 - "$TMP_QUADLET_DIR/openruyi-monitor.container" \
  "$HOME/.config/containers/systemd/openruyi-monitor.container" <<'PY'
from pathlib import Path
import sys
with Path(sys.argv[2]).open('x') as stream:
    stream.write(Path(sys.argv[1]).read_text())
PY
systemctl --user daemon-reload
systemctl --user start openruyi-monitor.service
journalctl --user -u openruyi-monitor.service -f
```

Configure user linger with the administrator. Do not use ordinary `systemctl
--user enable` for a generated Quadlet service; the template's Install section
handles activation. Do not leave an old `podman run` instance or host collection
timer running. Process restart policy and image health are different: this unit
does not kill the service merely because a probe fails.

## Verify HTTP and collection

The unit publishes only `127.0.0.1:18730`; API port 18731 stays private. Use
[the Caddy example](../deploy/Caddyfile.example) on the **host**, not another
container's localhost. Replace the domain and arrange DNS, ports 80/443, and
any private-site authentication/network restrictions outside the application.

- `/livez`: Node → FastAPI HTTP chain only.
- `/readyz`: a prepared snapshot is readable, possibly degraded.
- `/api/v2/status`: collection coverage and timestamp progression.

An empty data volume may be live before ready. Degraded is not fully healthy;
do not create fake snapshots to satisfy probes.

The base image does not include the optional `/opt/cve` scanner. Vendor/product
security checks require a controlled scanner and fresh `/data/cve` database;
the adapter enforces its two-day age limit. Until supplied, those checks remain
unavailable/error, not “no vulnerabilities.” OSV checks remain independent.

## Backup

Use SQLite's Backup API while the service runs; `cp` of a live database and API
exports are not recovery backups:

```sh
python3 deploy/backup-snapshot.py \
  --db "$DATA_DIR/state/tracker.sqlite3" \
  --output "$BACKUP_DIR/tracker-$(date -u +%Y%m%dT%H%M%SZ).sqlite3" \
  --timeout-seconds 30
```

The output directory must exist. The command refuses any existing output,
validates the snapshot, and publishes a 0600 file without overwriting concurrent
backups. A failed command is not success even if publication preceded a storage
sync error. Back up config/credentials and the image identifier separately;
arrange at least one independent storage copy and an explicit retention policy.

## Upgrade and rollback

Record the image ID/tag, config directory, and backup path. Build/test the new
image and prepare a new config. Take an online backup, stop the old instance,
then run preflight against the real config/data mounts before switching the
unit. This order avoids relabeling a live container's private SELinux volumes.
Retain data; never run two complete collectors against it.

For code rollback, stop the new instance and restore the previous tested image
and matching config. Do not automatically rewind observations. Database restore
is a separate explicit operation while writers are stopped; assess compatibility
for schema changes instead of assuming an arbitrary old image can read new data.

## Target-host acceptance

The operator must verify on the target host: native isolation/SELinux, real
provider timestamp progression, HTTPS, operation after logout, reboot recovery,
and backup restoration into an **independent test directory**. Offline smoke
proves entrypoint/permissions/HTTP/persistence/stop behavior, not these host and
external-service properties.
