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
# A disposable test layer leaves the application image free of test tooling.
TEST_IMAGE="localhost/openruyi-monitor-check:$$"
trap '"$ENGINE" image rm "$TEST_IMAGE" >/dev/null 2>&1 || true' EXIT
if [ "$ENGINE" = podman ]; then
  "$ENGINE" build --format docker --build-arg "RUNTIME_IMAGE=$IMAGE" -t "$TEST_IMAGE" -f "$ROOT/deploy/Containerfile.check" "$ROOT"
else
  "$ENGINE" build --build-arg "RUNTIME_IMAGE=$IMAGE" -t "$TEST_IMAGE" -f "$ROOT/deploy/Containerfile.check" "$ROOT"
fi
# Host-installer tests execute temporary command shims. Declare exec explicitly
# for this offline test harness; production mounts and worker confinement stay unchanged.
"$ENGINE" run "$@" --rm --network none --read-only --cap-drop=all \
  --security-opt=no-new-privileges --user 10001:10001 \
  --tmpfs /tmp:rw,nosuid,nodev,exec,size=128m \
  -v "$ROOT:/testsrc:ro,Z" --entrypoint sh "$TEST_IMAGE" -c '
    set -eu
    unset TRACKER_CONFIG TRACKER_DB TRACKER_SPEC_REPO HOST PORT API_PORT
    export PYTHONDONTWRITEBYTECODE=1
    export HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hypothesis
    cd /testsrc
    python scripts/check-architecture.py
    python scripts/api-types.py --check
    cd backend
    python -m pytest -q -p no:cacheprovider --junitxml=/tmp/results.xml
    python -c '\''import xml.etree.ElementTree as E; r=E.parse("/tmp/results.xml"); assert not r.findall(".//skipped"), "Skipped tests are not allowed in the native release gate"'\''
    cd /app/frontend
    node /testsrc/frontend/tests/render.mjs
  '
