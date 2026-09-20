#!/bin/sh
# Restore the previous release, preserving the shared snapshot.
# SPDX-FileCopyrightText: (C) 2026 Institute of Software, Chinese Academy of Sciences (ISCAS)
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
set -eu
BASE=${TRACKER_HOME:-"$HOME/apps/openruyi-tracker"}
WEB_HOST=${HOST:-127.0.0.1}
WEB_PORT=${PORT:-18730}
if [ ! -f "$BASE/previous-release" ]; then echo "No previous release recorded" >&2; exit 1; fi
PREVIOUS=$(cat "$BASE/previous-release")
case "$PREVIOUS" in "$BASE"/releases/*) ;; *) echo "Invalid previous release path" >&2; exit 1;; esac
test -f "$PREVIOUS/frontend/dist/server/entry.mjs"
ln -sfn "$PREVIOUS" "$BASE/current.next"
mv -Tf "$BASE/current.next" "$BASE/current"
if systemctl --user restart openruyi-tracker-api.service openruyi-tracker-web.service; then
  printf 'restart exit 0\n'
else
  code=$?
  printf 'restart exit %s; checking actual state\n' "$code" >&2
fi
systemctl --user is-active openruyi-tracker-api.service openruyi-tracker-web.service
attempt=0
until curl --fail --silent --show-error --max-time 5 "http://$WEB_HOST:$WEB_PORT/healthz"; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 10 ] || exit 1
  sleep 1
done
printf 'Restored release: %s\n' "$PREVIOUS"
