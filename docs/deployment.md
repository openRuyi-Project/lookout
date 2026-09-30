# Deployment

The preferred host is Fedora with Rootless Podman and user-level Quadlet. Lookout
runs one web/API/collector container; the host schedules image updates and backups.
The web port binds to host loopback. HTTPS, authentication and external access
belong to a separate proxy.

## Requirements

| Requirement | Check or responsibility |
|---|---|
| Linux x86_64, cgroup v2, Landlock ABI 6+ and seccomp | The published image targets linux/amd64. Installation probes the native RPM sandbox; unsupported isolation stops startup. |
| Rootless Podman, Quadlet, a user systemd manager and Python 3.11+ | The service account needs subordinate UID/GID ranges and linger. The image supplies application dependencies. |
| Writable local persistent storage | Keep the same data directory across updates. It contains SQLite, the SPEC repository and provider caches; do not use tmpfs or a network filesystem. |
| A trusted image and reachable providers | Public GHCR permits anonymous pulls. OBS, Git and upstream providers also need network access. |
| Private configuration and backups | Maintain free space, backup retention and an independent backup copy. Local backups do not protect against host loss. |

Only one application may write a data directory. The image runs as UID/GID 10001;
Rootless keep-id maps the service account to that identity. Configuration and the
container root are read-only. Do not add privileged mode, unconfined seccomp,
world-writable permissions or a container-engine socket mount.

## Install on Fedora

### 1. Prepare the service account

The host administrator installs prerequisites and enables linger, replacing
`SERVICE_USER` with the dedicated non-root account:

```sh
sudo dnf install podman python3 curl
sudo loginctl enable-linger SERVICE_USER
```

As that account, these checks must report `true` and `yes`:

```sh
podman info --format '{{.Host.Security.Rootless}}'
loginctl show-user "$USER" --property=Linger --value
```

Run the following blocks in the same Bash session as the service account.

### 2. Obtain the image and host tools

Choose a new instance directory. For Docker, use `ENGINE=docker` in this block,
then follow [Docker](#docker) instead of the Podman installation steps.

```sh
set -euo pipefail
umask 077
ENGINE=podman
NAME=openruyi-lookout
PORT=18730
IMAGE=ghcr.io/openruyi-project/lookout:main
ROOT="$HOME/.local/share/lookout"
UNIT="$HOME/.config/containers/systemd/$NAME.container"
mkdir -p "$ROOT"
test ! -e "$ROOT/tools"
"$ENGINE" pull "$IMAGE"
IMAGE_ID=$("$ENGINE" image inspect --format '{{.Id}}' "$IMAGE")
HELPER="lookout-tools-$$"
"$ENGINE" create --name "$HELPER" --network none --entrypoint /bin/true "$IMAGE_ID"
trap '"$ENGINE" rm "$HELPER" >/dev/null' EXIT
"$ENGINE" cp "$HELPER:/app/deploy" "$ROOT/tools"
"$ENGINE" rm "$HELPER"
trap - EXIT
```

The GHCR package must be Public for anonymous access. Private packages require
engine registry credentials, not a GitHub API key for version checks.

### 3. Install

```sh
python3 "$ROOT/tools/install.py" \
  --engine podman --image "$IMAGE_ID" --name "$NAME" \
  --directory "$ROOT" --port "$PORT" --network pasta \
  --auto-update "$IMAGE" | tee "$ROOT/installation.json"
```

Installation creates private configuration and persistent data, validates the
runtime and Quadlet, then waits for the service to become ready. Existing
installation destinations are refused. A failed installation retains its files;
inspect the error and journal rather than rerunning initialization over them.

```text
$ROOT/tools/            host tools
$ROOT/config/           operator configuration
$ROOT/data/             database, SPEC repository, caches
$ROOT/backups/          backups and upgrade records
$ROOT/installation.json initial image and paths
$UNIT                   application Quadlet
~/.config/systemd/user/$NAME-{update,backup}.{service,timer}
```

The image update timer checks roughly once a minute, with jitter. The backup
timer runs daily. Both serialize with upgrades. Collector schedules remain inside
the application.

To adopt existing state, stop its writer and back it up first. Add
`--data /absolute/existing/data --config /absolute/reviewed/config` to installation;
that data is reused rather than copied. Use image upgrades, not installation,
for subsequent releases.

### 4. Verify and take the first backup

```sh
systemctl --user status "$NAME.service" "$NAME-update.timer" "$NAME-backup.timer" --no-pager
curl --fail "http://127.0.0.1:$PORT/livez"
curl --fail "http://127.0.0.1:$PORT/readyz"
curl --fail "http://127.0.0.1:$PORT/api/v2/status"
python3 "$ROOT/tools/maintain.py" --unit "$UNIT" --backup-dir "$ROOT/backups"
python3 "$ROOT/tools/maintain.py" --unit "$UNIT" --status "$ROOT/backups"
```

The service and both timers must be active; the backup status must report
`"ok": true`. Check provider timestamps again after their next scheduled polls.

| Endpoint | Meaning |
|---|---|
| `/livez` | Node → FastAPI request chain works. |
| `/readyz`, or compatibility `/healthz` | The snapshot and its projection are readable. HTTP 200 may still report `degraded`. |
| `/api/v2/status` | Collection timestamps, coverage and failures. |

Initial collection is asynchronous. Readiness may initially be 503; installation
waits for it within a bounded startup budget. Real provider access, persistence
after logout and reboot recovery need acceptance on the target host.

## Operations

### Configuration and ports

**Image updates do not update operator configuration or packaged version rules.**
Review and promote those changes separately using [Configuration](../config/README.md).
SQLite stays in the same data directory; compatible updates reuse observations
and indexes. Supported storage-format changes are backed up and migrated;
unsupported changes refuse the upgrade.

| Setting | Entry point |
|---|---|
| Port | `install.py --port`; later change `PublishPort=127.0.0.1:PORT:8080` in `$UNIT` |
| Resource limits | `--memory 8g --cpus 4 --pids-limit 512`; later edit the Quadlet |
| Monitor proxy | `--env TRACKER_MONITOR_PROXY=URL` |
| Host-local proxy | `--network pasta:-T,7890 --env TRACKER_MONITOR_PROXY=http://127.0.0.1:7890` |
| OBS, Git, monitor schedules and identities | `$ROOT/config/` |
| Provider credentials | Private keyfiles, not frontend `PUBLIC_*` values |

Before changing the Quadlet or application configuration,
[pause scheduled jobs](#pause-scheduled-jobs). For a prepared configuration directory,
change the `/config:ro,Z` volume path in `$UNIT`; keep `/data` unchanged. After editing,
set `PORT` to the selected host port and run:

```sh
systemctl --user daemon-reload
systemctl --user restart "$NAME.service"
curl --fail "http://127.0.0.1:$PORT/readyz"
```

Resume only the timers that were active before the change; leave the update timer
stopped if the image is deliberately pinned.

Resource limits are defaults, not measured minimums. Directory paths cannot
contain colons; installer unit values cannot contain whitespace, quotes or
systemd `%` specifiers. Colons are valid in proxy URLs and network parameters.
No `.env` file is loaded implicitly.

`TRACKER_MONITOR_PROXY` does not configure Git, OBS or host-side registry pulls.
Configure each proxy at its boundary. For a host-local registry proxy, run:

```sh
systemctl --user edit "$NAME-update.service"
```

Add the required proxy addresses, for example:

```ini
[Service]
Environment=HTTP_PROXY=http://127.0.0.1:7890
Environment=HTTPS_PROXY=http://127.0.0.1:7890
```

Then run `systemctl --user daemon-reload`. Keep credentials in private files,
not shell history or public environment values.

Only web port 8080 is published, on host loopback; API 18731 stays internal. The
[Caddy example](../deploy/Caddyfile.example) is for a host proxy. Port or ingress
changes do not require a frontend rebuild.

The base image omits the optional `/opt/cve` scanner. Vendor/product coverage
requires its scanner and a fresh `/data/cve` database; otherwise it remains
unavailable/error. OSV-backed checks work independently.

### Image upgrades

`main` follows successful CI builds. A release tag `vX.Y.Z`, a
`sha-<full-commit>` tag, or `@sha256:<registry-digest>` selects a published build.
Unchanged images do not restart the application. A changed image has a short
stop/start interval; this is **not zero-downtime deployment**.

For a fixed image, enter its published release, commit tag or digest reference:

```sh
read -r -p 'Published image reference: ' SELECTED_IMAGE
systemctl --user stop "$NAME-update.timer"
python3 "$ROOT/tools/upgrade.py" --image "$SELECTED_IMAGE" \
  --unit "$UNIT" --backups "$ROOT/backups" --apply
```

An active backup/update lock rejects the command; retry after that job finishes.
The upgrade preserves the data/config mounts, port, environment and resource
limits. It records the prior image, unit and backup under `$ROOT/backups/upgrade-*`.
If the new image fails, the old image resumes only when it can read the resulting
data. Otherwise the instance stays stopped. Arbitrary downgrade compatibility is
not guaranteed; restoring an older database is a separate data-loss decision.

To resume the configured registry channel:

```sh
systemctl --user start "$NAME-update.timer"
```

### Backups and failures

Repeat the backup and status commands from [installation verification](#4-verify-and-take-the-first-backup)
for an on-demand backup. Copy the database backups, private configuration and
image references to independent storage. Set retention and monitor free space.
Do not `cp` a live SQLite database; API export is not a database backup.

Inspect failures without triggering collection or upgrades:

```sh
journalctl --user -u "$NAME.service" -u "$NAME-update.service" \
  -u "$NAME-backup.service" --no-pager -n 80
systemctl --user status "$NAME-update.timer" "$NAME-backup.timer" --no-pager
```

### Pause scheduled jobs

Before replacing host tools or cutting over a recovery instance, stop new jobs
and require existing ones to have finished:

```sh
systemctl --user stop "$NAME-update.timer" "$NAME-backup.timer"
for JOB in "$NAME-update.service" "$NAME-backup.service"; do
  case "$(systemctl --user show "$JOB" --property=ActiveState --value)" in
    inactive|failed) ;;
    *) printf 'Wait for %s to finish, then retry.\n' "$JOB" >&2; exit 1 ;;
  esac
done
```

### Refresh host tools

Image upgrades do not replace `$ROOT/tools` or host service files. When a release
requires new deployment tools, [pause scheduled jobs](#pause-scheduled-jobs), then
extract that trusted image's tools into a new directory:

```sh
read -r -p 'Published tools image reference: ' TOOLS_IMAGE
podman pull "$TOOLS_IMAGE"
TOOLS_ID=$(podman image inspect --format '{{.Id}}' "$TOOLS_IMAGE")
NEW_TOOLS="$ROOT/tools-$(date -u +%Y%m%dT%H%M%SZ)"
test ! -e "$NEW_TOOLS"
HELPER="lookout-tools-$$"
podman create --name "$HELPER" --network none --entrypoint /bin/true "$TOOLS_ID"
trap 'podman rm "$HELPER" >/dev/null' EXIT
podman cp "$HELPER:/app/deploy" "$NEW_TOOLS"
podman rm "$HELPER"
trap - EXIT
python3 "$NEW_TOOLS/upgrade.py" --help
python3 "$NEW_TOOLS/maintain.py" --help
printf 'New tools: %s\n' "$NEW_TOOLS"
systemctl --user edit --full "$NAME-update.service" "$NAME-backup.service"
```

In both services, change only the script directory in `ExecStart` to the printed
absolute path. Preserve their arguments and proxy settings. Then reload, inspect
the resolved services and resume their timers:

```sh
systemctl --user daemon-reload
systemctl --user cat "$NAME-update.service" "$NAME-backup.service"
systemctl --user start "$NAME-update.timer" "$NAME-backup.timer"
```

Keep the old tools until scheduled jobs succeed. This does not replace the
application's data or restart its service.

### Restore into a separate instance

Use a verified SQLite backup, its matching configuration and a compatible
published image. Keep the original data intact. The following creates an
independent recovery instance on a free loopback port:

```sh
read -r -p 'SQLite backup path: ' BACKUP
read -r -p 'Matching configuration directory: ' RESTORE_CONFIG
read -r -p 'Matching published image reference: ' RESTORE_IMAGE
read -r -p 'Free recovery port: ' RESTORE_PORT
test -f "$BACKUP"
test -f "$RESTORE_CONFIG/tracker.toml"
podman pull "$RESTORE_IMAGE"
RESTORE_ID=$(podman image inspect --format '{{.Id}}' "$RESTORE_IMAGE")
RESTORE_DIGEST=$(podman image inspect --format '{{ index .RepoDigests 0 }}' "$RESTORE_IMAGE")
RECOVERY=$(mktemp -d "$ROOT/recovery.XXXXXXXX")
RECOVERY_NAME="$NAME-recovery-$(basename "$RECOVERY" | cut -d. -f2)"
mkdir -p "$RECOVERY/data/state"
install -m 0600 "$BACKUP" "$RECOVERY/data/state/tracker.sqlite3"
HELPER="lookout-recovery-tools-$$"
podman create --name "$HELPER" --network none --entrypoint /bin/true "$RESTORE_ID"
trap 'podman rm "$HELPER" >/dev/null' EXIT
podman cp "$HELPER:/app/deploy" "$RECOVERY/tools"
podman rm "$HELPER"
trap - EXIT
python3 - "$RECOVERY/data/state/tracker.sqlite3" <<'PY'
from contextlib import closing
from pathlib import Path
import sqlite3, sys
with closing(sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + '?mode=ro', uri=True)) as db:
    if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise SystemExit('backup integrity check failed')
PY
python3 "$RECOVERY/tools/install.py" \
  --engine podman --image "$RESTORE_ID" --name "$RECOVERY_NAME" \
  --directory "$RECOVERY/instance" --data "$RECOVERY/data" \
  --config "$RESTORE_CONFIG" --port "$RESTORE_PORT" --network pasta \
  --auto-update "$RESTORE_DIGEST"
curl --fail "http://127.0.0.1:$RESTORE_PORT/readyz"
```

The installer checks storage compatibility and the native runtime before startup.
Verify the recovered observations, not just HTTP status. Supply the same required
proxy/network options as the original installation. Recovery jobs use the resolved
image digest and their own backup directory; changing their channel is a separate
operator choice.

For a production cutover, [pause the original scheduled jobs](#pause-scheduled-jobs),
then stop the original service and edit the recovery port:

```sh
systemctl --user stop "$NAME.service"
RECOVERY_UNIT="$HOME/.config/containers/systemd/$RECOVERY_NAME.container"
vi "$RECOVERY_UNIT"
```

Change `PublishPort` to `127.0.0.1:$PORT:8080`, substituting the original numeric
port. Edit the Quadlet, not the generated `.service`. Then:

```sh
systemctl --user daemon-reload
systemctl --user restart "$RECOVERY_NAME.service"
curl --fail "http://127.0.0.1:$PORT/readyz"
```

Record the recovery instance's new name, paths and backup directory. Preserve the
stopped original until acceptance; restoring this older snapshot loses observations
made after the backup. Do not overwrite the original database or start its timers.

## Docker

Obtain the tools using `ENGINE=docker` in the [extraction block](#2-obtain-the-image-and-host-tools).
Docker uses explicit config/data/backup volumes rather than the Podman instance
paths. Access to the Docker daemon is administrative.

```sh
python3 "$ROOT/tools/install.py" --engine docker --image "$IMAGE_ID" \
  --name "$NAME" --port "$PORT"
mkdir -p "$ROOT/backups"
docker inspect --format '{{.State.Status}} {{.Config.User}}' "$NAME"
curl --fail "http://127.0.0.1:$PORT/readyz"
python3 "$ROOT/tools/maintain.py" --container "$NAME" --backup-dir "$ROOT/backups"
python3 "$ROOT/tools/maintain.py" --container "$NAME" --status "$ROOT/backups"
```

For an image update:

```sh
python3 "$ROOT/tools/upgrade.py" --image "$IMAGE" \
  --container "$NAME" --backups "$ROOT/backups" --apply
```

For configuration or a port change, use `maintain.py --container "$NAME"` with
`--config /absolute/reviewed/config` or `--port PORT`. The application runs as UID
10001; only new-volume initialization uses a constrained root helper. Preserve
`$NAME-config`, `$NAME-data` and `$NAME-backups`; do not delete volumes or substitute
anonymous ones during upgrades.

Docker does not install the Podman timers. Schedule `upgrade.py` and `maintain.py`
on the host as the same engine owner, with stable `HOME`/`XDG_STATE_HOME`. See
[Image checks](../CONTRIBUTING.md#image-checks) for contributor validation.
