# Deployment

The primary deployment is one Linux container: the API, web server, and three
periodic collectors run together, without host collection timers or an external
scheduler. The Fedora 43 image supplies native RPM bindings and Node. Native SPEC
parsing also requires host-kernel Landlock ABI 6 or newer and seccomp support; it
fails closed when confinement is unavailable. Run rootless, or as a non-root
container user with writable data mounts, with all capabilities dropped. The
source checkout is build input, not mutable runtime state.

Python wheels are built in a separate stage. The runtime keeps RPM bindings,
macro packages and shared libraries, not the compiler/development toolchain.

Run from the repository root with Python 3.11+ and Podman. `--format docker` is required here:
Podman's default OCI format discards the image HEALTHCHECK. Docker users should
use `docker build` **without** `--format docker`, and replace `podman run/logs`
with `docker run/logs`:

```sh
# Initialize once; an existing destination is an error.
python3 deploy/init-config.py runtime-config
mkdir -p data
podman build --format docker -f Containerfile -t openruyi-tracker:local .
podman run -d --name openruyi-tracker --restart=unless-stopped \
  --stop-timeout=20 --cap-drop=all --security-opt=no-new-privileges \
  --read-only --tmpfs /tmp:rw,nosuid,nodev,size=128m \
  -p 127.0.0.1:18730:8080 \
  -v "$PWD/runtime-config:/config:ro,Z" -v "$PWD/data:/data:Z" \
  openruyi-tracker:local
curl --fail http://127.0.0.1:18730/healthz
podman logs openruyi-tracker
```

`/config/tracker.toml` and `versions/nvchecker.toml` are operator-owned
and read-only in the container. `/data/state/tracker.sqlite3` and the full bare SPEC
clone `/data/spec-full.git` persist independently of the image. Do not run native
host collectors concurrently against that database. Use a dedicated volume
location; on SELinux hosts `:Z` labels it for this container. For a LAN-facing
service, explicitly change the published host address and configure access control.

The API/web start without waiting for a full git clone. Initialization is bounded
by `[spec].fetch_timeout_seconds` (default 300 seconds) and retries after 60 seconds
on failure; timeout and exit status remain visible in the container log. A new empty volume may
return HTTP 503 until the first OBS snapshot is available. `/healthz` checks
snapshot readiness, not whether every external source is fresh; inspect
`/api/v1/status` and logs for collection state. Restart the container after editing
configuration so its scheduler reloads interval changes. `TRACKER_CONFIG`,
`TRACKER_DB`, and `TRACKER_SPEC_REPO` override the respective paths. `HOST` changes
the web bind address (default `0.0.0.0`); networking and proxy environment variables
remain deployment-owned. `[spec].url`
and `[spec].branch` select the managed repository origin and branch; defaults are
the official openRuyi repository and `main`. Network/proxy settings belong to the
environment; the application contains no hard-coded proxy or private host address.

For an update, build a new image tag before stopping the existing container, then
recreate it with the same mounts/port and new tag. Keep the old image and take a
SQLite backup before upgrades. For **code rollback**, stop/remove only this
application's container and rerun the command with the previous image tag, retaining
both mounted directories. Never delete the data volume. Code rollback does not
rewind collection observations; restoring a data backup is a separate explicit
operator action. The command above needs the host container service enabled for
reboot restart; this project does not install or alter host-global services.


The initializer validates the complete configuration before publishing it. See
[configuration changes](../config/README.md#推广到运行配置) for upgrades to rules.

Native SPEC confinement requires Landlock ABI 6+ and seccomp; unsupported kernels
report an error rather than executing a SPEC without confinement. See
[the security model](design.md#native-spec-confinement).
