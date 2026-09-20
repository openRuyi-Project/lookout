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


class IO:
    def __init__(self, cache=None, *, client=None, ttl=21600):
        self.cache = Path(cache) if cache else None
        self.client = client or httpx.Client(timeout=15, follow_redirects=False,
            proxy=os.environ.get('TRACKER_MONITOR_PROXY') or None)
        self.owns_client = client is None
        self.ttl = ttl
        self.today = datetime.now(timezone.utc).date()
        self.memory, self.locks = {}, {}
        self.guard = threading.Lock()

    def close(self):
        if self.owns_client:
            self.client.close()

    def for_hosts(self, hosts):
        return ProviderIO(self, frozenset(hosts))

    def json(self, method, url, body=None):
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
            if cached and 0 <= now - cached.get('time', 0) < self.ttl:
                self.memory[key] = cached
                if cached.get('error'):
                    raise ValueError('provider request failed earlier in this run')
                return cached['data']
            try:
                with self.client.stream(method, url, json=body if method == 'POST' else None,
                                        headers={'User-Agent': 'openRuyi-Package-Monitor/0.1.0'}) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    deadline = time.monotonic() + 30
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 16 * 1024 * 1024 or time.monotonic() > deadline:
                            raise ValueError('provider response exceeds budget')
                        chunks.append(chunk)
                data = json.loads(b''.join(chunks))
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
    def __init__(self, owner, hosts):
        self.owner, self.hosts = owner, hosts
        self.today = owner.today

    def json(self, method, url, body=None):
        parsed = urlsplit(url)
        if (method not in ('GET', 'POST') or parsed.scheme != 'https'
                or parsed.hostname not in self.hosts or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment):
            raise ValueError('provider URL outside declared HTTPS hosts')
        return self.owner.json(method, url, body)
