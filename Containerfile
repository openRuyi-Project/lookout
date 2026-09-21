# openRuyi Package Monitor — single-container image.
#
# Fedora base: native `rpm` Python bindings, git, and node are first-class here, which
# is why the SPEC source and version comparison need no extra tooling. The image runs
# on compatible Linux kernels (see docs/deployment.md confinement requirements); the host
# distribution is independent of the Fedora base image. One supervisor process runs the API, the web
# server, and the periodic collectors — a container has no systemd user timers.
#
# openRuyi packaging repository: https://github.com/openRuyi-Project/openRuyi
# SPDX-FileCopyrightText: (C) 2026 Institute of Software, Chinese Academy of Sciences (ISCAS)
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0

# ---- frontend build stage -------------------------------------------------
FROM registry.fedoraproject.org/fedora:43 AS frontend
RUN dnf install -y nodejs npm && dnf clean all
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
ENV ASTRO_TELEMETRY_DISABLED=1
RUN npm run check && npm run build && npm prune --omit=dev

# ---- Python dependency build stage ----------------------------------------
FROM registry.fedoraproject.org/fedora:43 AS python-builder
# Build native pip extensions on the same Fedora/Python ABI as the runtime.
# Compilers and development headers never enter the final image.
RUN dnf install -y \
        python3 python3-pip python3-devel \
        gcc gcc-c++ make libcurl-devel openssl-devel autoconf automake libtool \
    && dnf clean all

COPY backend/requirements.lock /tmp/requirements.lock
RUN python3 -m venv --system-site-packages /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.lock \
    && rm -f /tmp/requirements.lock

# ---- runtime stage --------------------------------------------------------
FROM registry.fedoraproject.org/fedora:43
# Keep native rpm and the existing Python RPM macro surface for SPEC parsing.
# pycurl needs the shared curl/OpenSSL libraries, not their development headers.
RUN dnf install -y \
        python3 python3-rpm rpm-build \
        python-rpm-macros python3-rpm-macros pyproject-rpm-macros python3-rpm-generators \
        git nodejs libcurl openssl-libs libseccomp \
    && dnf clean all
COPY --from=python-builder /opt/venv/ /opt/venv/

WORKDIR /app
COPY backend/ /app/backend/
COPY config/ /app/config/
COPY deploy/ /app/deploy/
COPY --from=frontend /build/frontend/dist/ /app/frontend/dist/
COPY --from=frontend /build/frontend/node_modules/ /app/frontend/node_modules/
COPY frontend/server.mjs /app/frontend/server.mjs

# State and the managed SPEC clone live on a mounted volume, outside the image.
ENV TRACKER_CONFIG=/config/tracker.toml \
    TRACKER_DB=/data/state/tracker.sqlite3 \
    TRACKER_SPEC_REPO=/data/spec-full.git \
    PORT=8080 \
    GIT_TERMINAL_PROMPT=0 \
    PATH=/opt/venv/bin:/usr/bin:/bin
VOLUME /data
EXPOSE 8080

# The web server has no built-in auth; put access control in front of it if exposed.
# Health check uses python (always present) rather than adding curl to the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python3 -c "import os,urllib.request; host=os.environ.get('HOST') or '127.0.0.1'; host='127.0.0.1' if host in ('0.0.0.0','::') else host; host='['+host+']' if ':' in host and not host.startswith('[') else host; urllib.request.urlopen('http://%s:%s/livez' % (host,os.environ['PORT']), timeout=3).read()" || exit 1

ENTRYPOINT ["/opt/venv/bin/python", "/app/deploy/container-entrypoint.py"]
