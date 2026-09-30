# Deployment

Lookout runs one web/API/collector container. The host owns persistent storage,
service supervision, backups and image updates. HTTPS and external access belong
to a separate proxy. The application never updates itself or accesses the
container-engine socket.

## Requirements and ownership

| Requirement | Consequence |
|---|---|
| Linux x86_64; cgroup v2, Landlock ABI 6+ and seccomp | The published image is tested on linux/amd64. Startup probes the real RPM worker; unsupported isolation stops startup rather than falling back. |
| Docker Engine, or non-root Rootless Podman with Quadlet and a user systemd manager | Host tools need Python 3.11+. The image supplies Python 3.14, Node and RPM; no source build or pip install is required on the host. |
| An explicitly mounted, writable **local** data directory/volume | `/data` contains SQLite, the SPEC clone and provider caches. Never put it in an image layer, tmpfs or a network filesystem. Keep its identity across updates. |
| One running application per data directory | A lifetime lease rejects a second supervisor. Upgrade and backup jobs share a separate instance lock. |
| Private configuration and backup storage | Mount `/config` read-only. The container root is also read-only; UID/GID is 10001. Rootless keep-id maps the service owner to this UID. |
| A trusted image publisher and reachable registry | Installing a container executes its publisher's code. A digest pins bytes; it does not establish trust. Public GHCR packages need no consumer token. |
| Network access to configured OBS, Git and upstream providers | `/livez` does not establish provider coverage. Optional scanner credentials/databases are separate requirements. |
| Backup capacity, an independent backup copy and failure monitoring | Local automatic backups do not survive loss of the host. Retention and off-host copying remain the administrator's responsibility. |

An unattended host must keep its user manager running after logout (linger).
An administrator supplies Podman, subordinate UID/GID ranges and this permission;
the installer neither runs sudo nor changes host security policy. Do not use
`--privileged`, unconfined seccomp, world-writable directories or a socket mount.

### What changes on upgrade

| Object | Owner and update behavior |
|---|---|
| Image | CI publishes the **same tested image**. The host resolves the selected channel to an immutable ID before switching. |
| Configuration | The operator owns the mounted files. Defaults are copied at installation, **not overwritten by image updates**. New packaged rules/policies require a reviewed configuration promotion; see [Configuration](../config/README.md). |
| SQLite | Reused in place. Same-format updates do not recreate tables or indexes. A supported schema transition gets a backup and explicit migration; unsupported formats stop the upgrade. |
| Observations | Reused by query/interpretation fingerprints. Poll timestamps and software releases are not reasons to discard unchanged evidence. Changed monitor inputs may require rechecking. |
| Read model | An in-memory projection, rebuilt from SQLite after startup. Readiness waits for it. It is not a second database and is not backed up. |
| Host tools | Extracted from the trusted image. Update them deliberately when the deployment contract changes; replacing an application image does not replace host scripts or units. |

`main` follows successful main-branch builds. `vX.Y.Z` selects a release tag;
`sha-<full-commit>` or `@sha256:<registry-digest>` selects one published build.
Failed CI does not publish `main`; failed pulls leave the running service alone.
An unchanged image is checked without backup, migration or restart.

One container owns serving and writing. A changed image therefore has a short
stop/start interval; **this is not zero-downtime deployment**. True zero downtime
requires a separate read-only serving role, schema compatibility across both
versions and proxy switching. No second writer is started to disguise this limit.

## Fedora: consume GitHub without a checkout

These commands use Bash, a non-root service account and the public image
`ghcr.io/openruyi-project/lookout:main`. Forks replace `IMAGE`; the tools do not
embed a repository owner. Do not execute these as root.

### 1. Prepare the host

The host administrator installs prerequisites and enables linger for the service
account (replace `SERVICE_USER`):

```sh
sudo dnf install podman python3 curl
sudo loginctl enable-linger SERVICE_USER
```

As that account, confirm Rootless Podman and linger:

```sh
podman info --format '{{.Host.Security.Rootless}}'  # true
loginctl show-user "$USER" --property=Linger --value  # yes
```

The installer also checks both conditions. A missing user bus or native sandbox
is a deployment failure, not a reason to disable isolation.

### 2. Pull and extract the host tools

Use a **new** instance directory. No source clone, local archive, image build or
workstation transfer is involved:

```sh
set -euo pipefail
umask 077
IMAGE=ghcr.io/openruyi-project/lookout:main
ROOT="$HOME/.local/share/lookout"
mkdir -p "$ROOT"
test ! -e "$ROOT/tools"
podman pull "$IMAGE"
IMAGE_ID=$(podman image inspect --format '{{.Id}}' "$IMAGE")
HELPER="lookout-tools-$$"
podman create --name "$HELPER" --network none --entrypoint /bin/true "$IMAGE_ID"
trap 'podman rm "$HELPER" >/dev/null' EXIT
podman cp "$HELPER:/app/deploy" "$ROOT/tools"
podman rm "$HELPER"
trap - EXIT
```

A `denied` response from GHCR is **not** a missing GitHub API key. The package
owner must make the package Public for anonymous consumption. For a private
installation, configure the engine's registry credentials explicitly.

### 3. Install the instance and scheduled jobs

```sh
python3 "$ROOT/tools/install.py" \
  --engine podman --image "$IMAGE_ID" --name lookout \
  --directory "$ROOT" --port 18730 --network pasta \
  --auto-update "$IMAGE" | tee "$ROOT/installation.json"
```

The installer copies image defaults into private configuration, checks native
runtime support, dry-runs Quadlet, installs the user service, starts it and waits
for liveness/readiness. Existing destinations are refused. Failure retains files
for diagnosis; it never deletes a database to make installation pass.

The resulting layout is stable, **not version-numbered**:

```text
~/.local/share/lookout/
  tools/               host tools from the image
  config/              operator configuration
  data/                SQLite, SPEC clone, provider caches
  backups/             consistent database backups and upgrade records
  units/               installation candidate
  installation.json    initial image and paths
~/.config/containers/systemd/lookout.container
~/.config/systemd/user/lookout-{update,backup}.{service,timer}
```

The update timer checks its configured registry channel roughly once a minute,
with jitter. Only a different image triggers the stopped-state transaction.
The backup timer takes one consistent SQLite backup daily. Both serialize with
upgrades; collectors retain their own schedules inside the application.

To import existing, stopped state rather than initialize an empty instance, add
`--data /absolute/existing/data --config /absolute/reviewed/config`. The installer
checks for containers using that data **before mounting or relabelling it**.
Stop the old writer first; save a consistent backup and its image/config identity.
The adopted data is not copied or reset. This is an operator migration, not an
alternative routine upgrade path.

### 4. Verify operation

```sh
systemctl --user status lookout.service lookout-update.timer lookout-backup.timer
curl --fail http://127.0.0.1:18730/livez
curl --fail http://127.0.0.1:18730/readyz
curl --fail http://127.0.0.1:18730/api/v2/status
journalctl --user -u lookout.service -u lookout-update.service -u lookout-backup.service
```

| Check | Meaning |
|---|---|
| `/livez` 200 | Node → FastAPI request chain works. |
| `/readyz` and compatibility `/healthz` 200 | A snapshot and its projection can be read; `degraded` remains meaningful. |
| Status API | Collection timestamps, coverage and failures. Confirm timestamps advance without a manual collector command. |
| Timer/service journal | Image changes, backup paths, failed pulls, unsupported migrations or rollback failures. |

Initial collection is asynchronous. A missing database is valid at first start;
readiness may initially be 503. Do not fabricate a successful snapshot. Validate
real provider access, logout persistence and reboot recovery on the target host.

## Administrator settings

| Setting | Rootless Podman | Docker |
|---|---|---|
| Host port | `install.py --port PORT`; later edit `PublishPort` in the Quadlet | `--port PORT`; later `maintain.py --port PORT` |
| Resource ceilings | `--memory 8g --cpus 4 --pids-limit 512`; later edit the unit | same install flags; later `docker update` |
| Outbound monitor proxy | `--env TRACKER_MONITOR_PROXY=URL`; provide a reachable private network | same environment flag |
| Local proxy example | `--network pasta:-T,7890 --env TRACKER_MONITOR_PROXY=http://127.0.0.1:7890` | supply an independently reachable proxy URL |
| OBS/SPEC/intervals, package identities/rules | mounted configuration | mounted configuration |
| Provider secrets | private keyfile; never frontend `PUBLIC_*` values | same |
| Image channel | `lookout-update.service --image`; disable the timer to pin | host scheduler invoking `upgrade.py` |

The ceilings are not measured minimums. Paths/unit values must not contain
whitespace, colons, quotes or systemd `%` specifiers. No `.env` file is implicitly
loaded. `TRACKER_MONITOR_PROXY` does not configure Git or OBS; Git's proxy is a
separate configuration choice. Secrets belong in private files, not shell history.

Only web port 8080 is published, on **host loopback**. API 18731 remains internal.
Domain, authentication and TLS are external responsibilities. The optional
[Caddy example](../deploy/Caddyfile.example) runs on the host, not another
container's localhost. No frontend rebuild is required for host port/ingress changes.

The base image does not contain the optional `/opt/cve` scanner or its database.
Vendor/product scanning needs an independently maintained scanner and fresh
`/data/cve` database. Missing coverage remains unavailable/error; OSV-backed
checks continue independently.

## Backup, upgrade and rollback

Create an online SQLite backup; ordinary `cp` of a running database is not one:

```sh
python3 "$ROOT/tools/maintain.py" \
  --unit "$HOME/.config/containers/systemd/lookout.container" \
  --backup-dir "$ROOT/backups"
python3 "$ROOT/tools/maintain.py" \
  --unit "$HOME/.config/containers/systemd/lookout.container" \
  --status "$ROOT/backups"
```

Copy backups, private configuration and image/channel identities to independent
storage. Establish retention and monitor free space; the tools deliberately do
not delete arbitrary backups or prune other applications' images. An API export
is not a database recovery backup. Test restoration into a separate directory.

For a deliberate image selection, stop the update timer before selecting a pin:

```sh
systemctl --user stop lookout-update.timer
python3 "$ROOT/tools/upgrade.py" --image ghcr.io/openruyi-project/lookout:main \
  --unit "$HOME/.config/containers/systemd/lookout.container" \
  --backups "$ROOT/backups" --apply
```

The transaction pulls/validates before stopping; records the running immutable
image; stops the writer; recovers a hot journal if present; backs up with the old
reader; runs the new image's migration/preflight; switches only the image; and
waits for readiness. It preserves configuration, data path, port, environment and
resource limits. Each transaction records its previous unit, image metadata and
result under `backups/upgrade-*`.

On failure, the old image is resumed **only if it can read the resulting data**.
If not, leave the instance stopped and inspect the transaction. Never silently
restore an older database over newer observations. Database restoration is a
separate, stopped operation with explicit data-loss consequences. Schema-changing
updates need compatibility review; arbitrary downgrade compatibility is not promised.

A registry channel failure does not require manual collection. Diagnose the host
update journal, native preflight or provider status at the responsible boundary.
To refresh host tools, extract `/app/deploy` from a selected trusted image into a
**new** directory and update the host unit paths; keep the old tools until verified.

## Docker consumption

Extract the tools from the pulled image as above, using `docker`. Then:

```sh
python3 tools/install.py --image ghcr.io/openruyi-project/lookout:main \
  --name lookout --port 18730
python3 tools/upgrade.py --image ghcr.io/openruyi-project/lookout:main \
  --container lookout --backups /absolute/private/backups --apply
```

Docker installation creates explicit `lookout-config`, `lookout-data` and
`lookout-backups` named volumes, with a shared instance identity. It refuses to
initialize existing names. Never run `docker compose down -v`, delete these
volumes or substitute anonymous volumes during an image change. Docker socket
access is administrative. The application runs as UID 10001; only initialization
of new volumes uses a constrained root helper.

Docker does not use the Podman user-systemd templates. Administrators schedule
`upgrade.py` and `maintain.py` on the host, as the same engine owner with a stable
`HOME`/`XDG_STATE_HOME`; the lock identity includes the daemon and instance.

## Publisher checks

CI tests the actual Docker entrypoint, native confinement, backend, API/SSR,
persistence, migration, failed-upgrade rollback and restore before publishing.
Only trusted main/tag runs publish; PR jobs have no registry-write permission.
The same tested image is transferred to the publishing job, **not rebuilt**.

For local contributors, run the existing checks against one explicitly labelled
image, not against a running production instance:

```sh
CONTAINER_ENGINE=docker deploy/check-image.sh IMAGE
CONTAINER_ENGINE=docker python3 deploy/smoke-image.py IMAGE
python3 deploy/smoke-release.py IMAGE
```

Publication currently targets linux/amd64. Additional architectures need their
own native confinement and entrypoint gates, not an untested platform declaration.
Package owners must make the GHCR package Public for anonymous consumption.
Host/HTTPS/provider acceptance and off-host restore drills remain distinct from CI.
