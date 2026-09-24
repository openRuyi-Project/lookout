"""Bounded provider HTTP with shared, dated cache. Never used by the API."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
import httpx
from .http_io import read_response


class IO:
    def __init__(self, cache=None, *, client=None, ttl=21600, workers=4):
        self.cache = Path(cache) if cache else None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(connect=15, read=15, write=15, pool=15),
            limits=httpx.Limits(max_connections=workers, max_keepalive_connections=workers),
            follow_redirects=False,
            proxy=os.environ.get('TRACKER_MONITOR_PROXY') or None)
        self.owns_client = client is None
        self.ttl = ttl
        self.today = datetime.now(timezone.utc).date()
        self.memory, self.locks = {}, {}
        self.guard = threading.Lock()

    def close(self):
        if self.owns_client:
            self.client.close()

    def for_hosts(self, hosts, *, max_age=None):
        return ProviderIO(self, frozenset(hosts), max_age)

    def json(self, method, url, body=None, *, max_age=None):
        key = hashlib.sha256(json.dumps([method, url, body], sort_keys=True).encode()).hexdigest()
        with self.guard:
            lock = self.locks.setdefault(key, threading.Lock())
        with lock:
            now = time.time()
            cached = self.memory.get(key)
            path = self.cache / (key + '.json') if self.cache else None
            if cached is None and path and path.is_file() and path.stat().st_size <= 32 * 1024 * 1024:
                try:
                    cached = json.loads(path.read_text())
                except (OSError, ValueError):
                    pass
            ttl = self.ttl if max_age is None else min(self.ttl, max_age)
            if cached and 0 <= now - cached.get('time', 0) < ttl:
                self.memory[key] = cached
                if cached.get('error'):
                    raise ValueError('provider request failed earlier in this run')
                return cached['data']
            try:
                deadline = time.monotonic() + 30
                with self.client.stream(method, url, json=body if method == 'POST' else None,
                                        headers={'User-Agent': 'openRuyi-Package-Monitor/0.1.0'}) as response:
                    response.raise_for_status()
                    body_bytes = read_response(response, max_bytes=16 * 1024 * 1024, deadline=deadline)
                data = json.loads(body_bytes)
                cached = {'time': now, 'data': data}
                if path:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temp = path.with_suffix('.tmp')
                    temp.write_text(json.dumps(cached, separators=(',', ':')))
                    temp.replace(path)
                self.memory[key] = cached
                return data
            except Exception:
                # A broken global KEV endpoint must not be retried for every package.
                self.memory[key] = {'time': now, 'error': True}
                raise


class ProviderIO:
    def __init__(self, owner, hosts, max_age=None):
        self.owner, self.hosts = owner, hosts
        self.max_age = max_age
        self.today = owner.today

    def json(self, method, url, body=None):
        parsed = urlsplit(url)
        if (method not in ('GET', 'POST') or parsed.scheme != 'https'
                or parsed.hostname not in self.hosts or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment):
            raise ValueError('provider URL outside declared HTTPS hosts')
        return self.owner.json(method, url, body, max_age=self.max_age)
