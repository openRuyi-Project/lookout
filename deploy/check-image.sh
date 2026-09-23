#!/bin/sh
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
set -eu
IMAGE=${1:?usage: deploy/check-image.sh IMAGE}
ENGINE=${CONTAINER_ENGINE:-podman}
case "$ENGINE" in docker|podman) ;; *) echo "CONTAINER_ENGINE must be docker or podman" >&2; exit 2;; esac
set --
if [ "$ENGINE" = podman ] && [ "$(podman info --format '{{.Host.Security.Rootless}}')" = true ]; then
  set -- --userns=keep-id:uid=10001,gid=10001
fi
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
# Host-installer tests execute temporary command shims. Declare exec explicitly
# for this offline test harness; production mounts and worker confinement stay unchanged.
"$ENGINE" run "$@" --rm --network none --read-only --cap-drop=all \
  --security-opt=no-new-privileges --user 10001:10001 \
  --tmpfs /tmp:rw,nosuid,nodev,exec,size=128m \
  -v "$ROOT:/testsrc:ro,Z" --entrypoint sh "$IMAGE" -c '
    set -eu
    unset TRACKER_CONFIG TRACKER_DB TRACKER_SPEC_REPO HOST PORT API_PORT
    export PYTHONDONTWRITEBYTECODE=1
    cd /testsrc
    python scripts/check-architecture.py
    python scripts/api-types.py --check
    cd backend
    python -m pytest -q -p no:cacheprovider --junitxml=/tmp/results.xml
    python -c '\''import xml.etree.ElementTree as E; r=E.parse("/tmp/results.xml"); assert not r.findall(".//skipped"), "Skipped tests are not allowed in the native release gate"'\''
    cd /app/frontend
    node /testsrc/frontend/tests/render.mjs
  '
