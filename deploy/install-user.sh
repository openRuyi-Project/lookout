#!/bin/sh
# Installs only this application's user units.
# SPDX-FileCopyrightText: (C) 2026 Institute of Software, Chinese Academy of Sciences (ISCAS)
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
set -eu
BASE=${TRACKER_HOME:-"$HOME/apps/openruyi-tracker"}
RELEASE=${1:?Usage: install-user.sh /absolute/release/directory}
case "$RELEASE" in "$BASE"/releases/*) ;; *) echo "Release must be inside $BASE/releases" >&2; exit 1;; esac
CONFIG=${TRACKER_CONFIG:-"$RELEASE/config/tracker.toml"}
UNIT_CONFIG=${TRACKER_CONFIG:-"$BASE/current/config/tracker.toml"}
WEB_HOST=${HOST:-127.0.0.1}
WEB_PORT=${PORT:-18730}
test -f "$RELEASE/frontend/dist/server/entry.mjs"
test -f "$BASE/state/tracker.sqlite3"
# Read and validate the release configuration before changing any live unit/link.
INTERVALS=$("$BASE/runtime/venv/bin/python" - "$RELEASE" "$CONFIG" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
sys.path.insert(0, str(root / 'backend'))
from tracker.config import load
config = load(sys.argv[2])
c, spec = config['collector'], config['spec']
# The SPEC git source is optional; '-' means no managed clone is configured.
print(c['obs_interval_seconds'], c['nvchecker_interval_seconds'], c['nvchecker_timeout_seconds'] + 300,
      spec['interval_seconds'], spec['fetch_timeout_seconds'] + 300, spec['repo'] or '-')
PY
)
set -- $INTERVALS
OBS_INTERVAL=$1
UPSTREAM_INTERVAL=$2
UPSTREAM_TIMEOUT=$3
SPEC_INTERVAL=$4
SPEC_TIMEOUT=$5
SPEC_REPO=$6
UNITS="$HOME/.config/systemd/user"
mkdir -p "$UNITS"
if [ -L "$BASE/current" ]; then readlink "$BASE/current" > "$BASE/previous-release"; fi
ln -sfn "$RELEASE" "$BASE/current.next"
mv -Tf "$BASE/current.next" "$BASE/current"
cat > "$UNITS/openruyi-tracker-api.service" <<EOF
[Unit]
Description=openRuyi package snapshot API
After=network.target
[Service]
Type=simple
WorkingDirectory=$BASE/current/backend
Environment=TRACKER_DB=$BASE/state/tracker.sqlite3
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$BASE/runtime/venv/bin/python -m uvicorn tracker.api:app --host 127.0.0.1 --port 18731 --no-access-log
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
UMask=0077
PrivateTmp=yes
ProtectSystem=strict
ReadOnlyPaths=$BASE/state
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
[Install]
WantedBy=default.target
EOF
cat > "$UNITS/openruyi-tracker-web.service" <<EOF
[Unit]
Description=openRuyi package table
After=network.target openruyi-tracker-api.service
Wants=openruyi-tracker-api.service
[Service]
Type=simple
WorkingDirectory=$BASE/current/frontend
Environment=HOST=$WEB_HOST
Environment=PORT=$WEB_PORT
Environment=TRACKER_API_URL=http://127.0.0.1:18731
Environment=ASTRO_TELEMETRY_DISABLED=1
ExecStart=/bin/sh -c 'if [ -f server.mjs ]; then exec "$BASE/runtime/node/bin/node" server.mjs; else exec "$BASE/runtime/node/bin/node" dist/server/entry.mjs; fi'
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
UMask=0077
PrivateTmp=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
[Install]
WantedBy=default.target
EOF
cat > "$UNITS/openruyi-tracker-collect.service" <<EOF
[Unit]
Description=Collect openRuyi package observations
After=network-online.target
[Service]
Type=oneshot
WorkingDirectory=$BASE/current/backend
Environment=PATH=$BASE/runtime/venv/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$BASE/runtime/venv/bin/python -m tracker.collector --only obs --config $UNIT_CONFIG --db $BASE/state/tracker.sqlite3
TimeoutStartSec=15min
SuccessExitStatus=2 75
NoNewPrivileges=yes
UMask=0077
PrivateTmp=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
EOF
cat > "$UNITS/openruyi-tracker-collect.timer" <<EOF
[Unit]
Description=Periodic openRuyi package collection
[Timer]
OnBootSec=1min
OnUnitInactiveSec=${OBS_INTERVAL}s
AccuracySec=1s
Persistent=true
Unit=openruyi-tracker-collect.service
[Install]
WantedBy=timers.target
EOF
cat > "$UNITS/openruyi-tracker-upstreams.service" <<EOF
[Unit]
Description=Check openRuyi upstream versions with nvchecker
After=network-online.target
[Service]
Type=oneshot
WorkingDirectory=$BASE/current/backend
Environment=PATH=$BASE/runtime/venv/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$BASE/runtime/venv/bin/python -m tracker.collector --only upstreams --config $UNIT_CONFIG --db $BASE/state/tracker.sqlite3
TimeoutStartSec=${UPSTREAM_TIMEOUT}s
SuccessExitStatus=2 75
NoNewPrivileges=yes
UMask=0077
PrivateTmp=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
EOF
cat > "$UNITS/openruyi-tracker-upstreams.timer" <<EOF
[Unit]
Description=Periodic openRuyi upstream checks
[Timer]
OnBootSec=2min
OnUnitInactiveSec=${UPSTREAM_INTERVAL}s
RandomizedDelaySec=5min
AccuracySec=10s
Persistent=true
Unit=openruyi-tracker-upstreams.service
[Install]
WantedBy=timers.target
EOF
# The SPEC git source is optional. Its units are written only when a managed clone is
# configured (spec.repo), so deployments without it stay clean.
SPEC_UNITS=""
if [ "$SPEC_REPO" != "-" ]; then
  SPEC_UNITS="openruyi-tracker-specs.timer"
  cat > "$UNITS/openruyi-tracker-specs.service" <<EOF
[Unit]
Description=Refresh openRuyi SPEC metadata and changelog from the managed git clone
After=network-online.target
[Service]
Type=oneshot
WorkingDirectory=$BASE/current/backend
Environment=PATH=$BASE/runtime/venv/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$BASE/runtime/venv/bin/python -m tracker.collector --only specs --config $UNIT_CONFIG --db $BASE/state/tracker.sqlite3
TimeoutStartSec=${SPEC_TIMEOUT}s
SuccessExitStatus=2 75
NoNewPrivileges=yes
UMask=0077
PrivateTmp=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
EOF
  cat > "$UNITS/openruyi-tracker-specs.timer" <<EOF
[Unit]
Description=Periodic openRuyi SPEC git refresh
[Timer]
OnBootSec=3min
OnUnitInactiveSec=${SPEC_INTERVAL}s
RandomizedDelaySec=5min
AccuracySec=10s
Persistent=true
Unit=openruyi-tracker-specs.service
[Install]
WantedBy=timers.target
EOF
fi
# Some hosts report a nonzero mutation exit despite applying the change. Never
# promote that exit to success: retain it and independently check loaded/enabled/active state.
control() {
  if systemctl --user "$@"; then
    printf 'systemctl %s: exit 0\n' "$*"
  else
    code=$?
    printf 'systemctl %s: exit %s; verifying observed state\n' "$*" "$code" >&2
  fi
}
control daemon-reload
SPEC_SERVICE=""
[ "$SPEC_REPO" != "-" ] && SPEC_SERVICE="$UNITS/openruyi-tracker-specs.service"
systemd-analyze --user verify "$UNITS/openruyi-tracker-api.service" "$UNITS/openruyi-tracker-web.service" "$UNITS/openruyi-tracker-collect.service" "$UNITS/openruyi-tracker-upstreams.service" $SPEC_SERVICE
control enable --now openruyi-tracker-api.service openruyi-tracker-web.service openruyi-tracker-collect.timer openruyi-tracker-upstreams.timer $SPEC_UNITS
control restart openruyi-tracker-api.service openruyi-tracker-web.service openruyi-tracker-collect.timer openruyi-tracker-upstreams.timer $SPEC_UNITS
systemctl --user is-enabled openruyi-tracker-api.service openruyi-tracker-web.service openruyi-tracker-collect.timer openruyi-tracker-upstreams.timer
systemctl --user is-active openruyi-tracker-api.service openruyi-tracker-web.service openruyi-tracker-collect.timer openruyi-tracker-upstreams.timer
attempt=0
until curl --fail --silent --show-error --max-time 5 "http://$WEB_HOST:$WEB_PORT/healthz"; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 10 ] || exit 1
  sleep 1
done
