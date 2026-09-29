# Deployment

One container runs Astro, FastAPI and scheduled OBS, SPEC, version and monitor
checks. `/config` is read-only; `/data` holds SQLite, the SPEC clone and caches.
Keep these across releases. Run **one writer per data directory** on local disk.

## Requirements

- Linux, cgroup v2, Landlock ABI 6+ and seccomp; verify with the image tests below.
- Docker Engine, or a non-root account with Rootless Podman, Quadlet and `busctl`.
- Python 3.11+ for host release tools. The image supplies Python 3.14 and Node.
- Private config and backup directories. Docker socket access is administrative;
  never mount it into the application. Do not use network storage for SQLite.

## Install a release with Docker

Use a release bundle for the host architecture, from a trusted publisher. If no
bundle is published, [build one](#build-and-test). Keep the bundle: it contains the
image, manifest and upgrade tools. Checksums detect corruption, not a compromised
publisher. In the unpacked directory:

```sh
sha256sum --check SHA256SUMS
python3 install.py --name openruyi-monitor --port 18730
curl --fail http://127.0.0.1:18730/livez
```

Installation creates named `openruyi-monitor-config`, `-data` and `-backups`
volumes; existing names are refused. The app uses UID/GID 10001, read-only
root/config, dropped capabilities and resource limits. Only initialization of
new volumes uses root. Failed installation retains its volumes for inspection.

To install reviewed settings instead of the defaults:

```sh
python3 install.py --name openruyi-monitor --port 28730 \
  --config /absolute/private/config --memory 8g --cpus 4 --pids-limit 512
```

Files are copied as the operator into a private config volume; symlinks are
rejected. Do not relax host permissions for container UID 10001. Defaults are
copied only at first install, never on upgrade.

## Administrator settings

| Setting | Docker | Rootless Quadlet |
|---|---|---|
| Host port (default 18730) | `install.py --port PORT`; change with `maintain.py --port PORT` | `PublishPort=127.0.0.1:PORT:8080` |
| Memory ceiling (8 GiB, not a measured minimum) | `--memory 8g`; `docker update --memory 8g NAME` | `Memory=8g` |
| CPU ceiling (four CPUs) | `--cpus 4`; `docker update --cpus 4 NAME` | `[Service] CPUQuota=400%` |
| Process ceiling (512, including collectors) | `--pids-limit 512`; `docker update --pids-limit 512 NAME` | `PidsLimit=512` |
| Monitor outbound proxy | `--env TRACKER_MONITOR_PROXY=URL` | `Environment=TRACKER_MONITOR_PROXY=URL` |
| SPEC clone path | `--env TRACKER_SPEC_REPO=/data/spec-full.git` | same variable in `Environment=` |
| Source repository/branch, OBS targets, intervals | mounted `tracker.toml` | mounted `tracker.toml` |
| Package identities and version rules | [Configuration](../config/README.md) | same |
| Provider secrets | private nvchecker keyfile; never `PUBLIC_*` variables | same |

Container ports stay web 8080 and private API 18731. Publish only the web port on
loopback. Domain, HTTPS, authentication and ingress belong to an independent host
proxy; the app does not discover or configure them. The optional
[Caddy example](../deploy/Caddyfile.example) runs **on the host**. Another
container's localhost is not the host. No frontend rebuild is needed for a port
or proxy change.

`TRACKER_MONITOR_PROXY` does not configure Git or OBS. Configure Git's proxy
separately when needed. An empty `TRACKER_SPEC_REPO` falls back to `[spec].repo`;
a managed clone also requires explicit `[spec].url` and `[spec].branch`.
No `.env` file is implicitly loaded. Keep secrets in private files, not shell history.

### Change Docker configuration

Export, edit and validate a copy; import it into a **new** volume without replacing
data. `maintain.py` preflights, restarts and restores the previous service on failure.

```sh
umask 077
mkdir /absolute/private/config-next
docker cp openruyi-monitor:/config/. /absolute/private/config-next/
# Edit config-next using the configuration guide, then:
python3 maintain.py --container openruyi-monitor --config /absolute/private/config-next
```

`--port PORT` can accompany `--config`. Existing resource/environment overrides
survive upgrades. For Quadlet, edit a new config directory and the unit, then
preflight and restart with the operator's service tools.

## Rootless Podman / Quadlet

Use a dedicated account; ask the host administrator to enable linger. Run the
following from the verified bundle. `ROOT` must be a new absolute directory;
paths must not contain spaces, colons, quotes or systemd `%` specifiers.

```sh
set -eu
umask 077
ROOT="$HOME/services/openruyi-monitor"
mkdir -p "$HOME/services"
mkdir "$ROOT"
mkdir "$ROOT/config" "$ROOT/data" "$ROOT/backups" "$ROOT/units"
sha256sum --check SHA256SUMS
podman load --input image.tar
IMAGE=$(python3 -c 'import json; print(json.load(open("release.json"))["image"])')
podman run --rm --network none --read-only --cap-drop=all \
  --security-opt=no-new-privileges --userns=keep-id:uid=10001,gid=10001 \
  -v "$ROOT/config:/bootstrap:Z" --entrypoint /opt/venv/bin/python "$IMAGE" \
  -c 'import shutil; shutil.copytree("/app/config", "/bootstrap", dirs_exist_ok=True)'
```

Review `$ROOT/config`. The following uses the same identity, mounts and sandbox
as the service. Preflight may create directories but does not modify an existing
database or contact providers. A missing database is valid on first startup.

```sh
podman run --rm --network none --read-only --cap-drop=all \
  --security-opt=no-new-privileges --userns=keep-id:uid=10001,gid=10001 \
  --tmpfs /tmp:rw,nosuid,nodev,size=128m,mode=1777 \
  -v "$ROOT/config:/config:ro,Z" -v "$ROOT/data:/data:Z" \
  --entrypoint /opt/venv/bin/python "$IMAGE" -m tracker.runtime_checks \
  --config /config/tracker.toml --db /data/state/tracker.sqlite3
python3 - "$IMAGE" "$ROOT" <<'PY'
from pathlib import Path
import sys
image, root = sys.argv[1:]
values = {'IMAGE': image, 'CONFIG_DIR': root + '/config', 'DATA_DIR': root + '/data'}
if any(any(c.isspace() or c in "'\"%" for c in v) for v in values.values()):
    raise SystemExit('unsupported unit value')
if ':' in root or not Path(root).is_absolute():
    raise SystemExit('use an absolute directory without colons')
text = Path('openruyi-monitor.container.in').read_text()
for key, value in values.items():
    text = text.replace('@' + key + '@', value)
if '@' in text:
    raise SystemExit('unresolved template placeholder')
with (Path(root) / 'units/openruyi-monitor.container').open('x') as output:
    output.write(text)
PY
QUADLET_UNIT_DIRS="$ROOT/units" \
  /usr/lib/systemd/system-generators/podman-system-generator --user --dryrun
```

Use the host's actual generator path. Inspect the generated mounts, loopback port
and UID mapping; missing generator is not a passed check. Install without overwriting:

```sh
mkdir -p "$HOME/.config/containers/systemd"
(set -C; cat "$ROOT/units/openruyi-monitor.container" > \
  "$HOME/.config/containers/systemd/openruyi-monitor.container")
systemctl --user daemon-reload
systemctl --user start openruyi-monitor.service
journalctl --user -u openruyi-monitor.service
```

Quadlet `[Install]` handles activation; do not enable the generated service.
`:Z` is an exclusive SELinux label: stop the writer before another container
mounts its data for preflight/migration. Online backup runs inside the existing
container. Remove the old service or collection timer before starting a replacement.

## Verify operation

| Request | Meaning |
|---|---|
| `curl --fail http://127.0.0.1:18730/livez` | Node → FastAPI responds |
| `curl --fail http://127.0.0.1:18730/readyz` | Snapshot readable; may report `degraded`. `/healthz` is the same readiness check. |
| `curl --fail http://127.0.0.1:18730/api/v2/status` | Coverage, errors and collection timestamps; observe their progression |

An empty install can be live before ready. Degraded readiness is not complete
provider coverage. The optional `/opt/cve` scanner and fresh `/data/cve` database
must be provisioned separately for vendor/product security checks; missing or
older-than-two-days data stays unavailable. OSV checks are independent.

## Back up and upgrade

Create a verified online backup; `--status` exits nonzero for missing/old backups
or insufficient space. Default maximum age is 26 hours.

```sh
install -d -m 0700 /absolute/backups
python3 maintain.py --container openruyi-monitor \
  --output "/absolute/backups/tracker-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
python3 maintain.py --container openruyi-monitor --status /absolute/backups
```

Podman uses `--unit /absolute/path/openruyi-monitor.container` instead of
`--container NAME`. Schedule **backups**, not collection, daily with the host's
scheduler; arrange disk/backup failure alerts there. Keep config, credentials,
image identity and an independent storage copy too. SQLite's online Backup API
produces a validated 0600 file; copying a live database or API export does not.
Docker logs rotate at 10 MiB × 3; Podman uses host journald retention.

### Upgrade and recovery

From a verified **new** bundle, inspect the plan, then apply:

```sh
python3 upgrade.py --container openruyi-monitor --backups /absolute/backups
python3 upgrade.py --container openruyi-monitor --backups /absolute/backups --apply
```

The transaction checks image identity, stops the sole writer, recovers a hot
journal, backs up, performs a supported migration, preflights and starts the new
image. It preserves config/data, port, resources and environment. Unchanged query
fingerprints reuse observations and caches. Expect a short service interruption.

Each transaction saves the old runtime, image, backup and result in a new private
directory. On failure, it restarts the old image only if that image can read the
current data; otherwise it stays stopped. Inspect the transaction before retrying.
Never delete a journal or replace an unreadable database with an empty one.

For code rollback, stop writers and preflight the saved old image/config against
current data before restoring that runtime. If its storage format is incompatible:

1. Restore the verified backup into an **independent** data directory or volume.
2. Test the matching image on that copy: integrity, packages, evidence and readiness.
3. Explicitly choose whether to switch. Keep current data; restoring a backup loses
   later observations. Upgrades do not automatically rewind data or delete old volumes.

## Build and test

From a clean checkout of an explicit commit, on the target architecture:

```sh
VERSION=$(python3 -c 'import tomllib; print(tomllib.load(open("backend/pyproject.toml", "rb"))["project"]["version"])')
IMAGE="openruyi-monitor:$(git rev-parse --short=12 HEAD)"
docker build --build-arg SOURCE_REVISION="$(git rev-parse HEAD)" \
  --build-arg RELEASE_VERSION="$VERSION" --build-arg SOURCE_URL="${SOURCE_URL:-}" \
  -t "$IMAGE" -f Containerfile .
CONTAINER_ENGINE=docker deploy/check-image.sh "$IMAGE"
CONTAINER_ENGINE=docker python3 deploy/smoke-image.py "$IMAGE"
python3 deploy/smoke-release.py "$IMAGE"
CONTAINER_ENGINE=docker python3 deploy/release.py --image "$IMAGE" --output /new/release-dir
```

Podman builds need `--format docker` for HEALTHCHECK; image gates use
`CONTAINER_ENGINE=podman`. The release smoke tests Docker install/upgrade;
Quadlet additionally needs generator and isolated systemd tests. Release packaging
rejects dirty source or mismatched image revision/version. `SOURCE_URL` is optional
publisher metadata, not the website or monitored SPEC URL; CI supplies its own
repository identity.

Deploy the tested artifact, not a rebuild of moving `main`. Scan with a current
vulnerability database and record its coverage/age. Operator acceptance still
requires target-host isolation/SELinux, real provider timestamps, logout/reboot
survival, independent backup recovery and the chosen external access setup.
