"""Bounded provider HTTP with shared, dated cache. Never used by the API."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

import httpx

from tracker.providers.http import read_response


class IO:
    """Share request results and host pacing within one collection batch.

    Successful responses may survive in the disk cache; failures and Retry-After
    cooldowns live only in this instance. Injected clients remain caller-owned
    and must disable redirects and configure finite transport timeouts.
    """
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
        self.host_locks, self.next_request, self.cooldowns = {}, {}, {}

    def close(self):
        if self.owns_client:
            self.client.close()

    def for_hosts(self, hosts, *, max_age=None):
        return ProviderIO(self, frozenset(hosts), max_age)

    def wait_for_host(self, host, interval):
        if not isinstance(interval, (int, float)) or not math.isfinite(interval) or not 0 <= interval <= 60:
            raise ValueError('invalid provider request interval')
        with self.guard:
            lock = self.host_locks.setdefault(host, threading.Lock())
        with lock:
            if self.cooldowns.get(host, 0) > time.monotonic():
                raise ValueError('provider requested a cooldown')
            delay = self.next_request.get(host, 0) - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if interval:
                self.next_request[host] = time.monotonic() + interval

    def defer_host(self, host, retry_after):
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            try:
                delay = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = 60
        if not math.isfinite(delay):
            delay = 60
        with self.guard:
            self.cooldowns[host] = time.monotonic() + max(60, delay)

    def json(self, method, url, body=None, *, max_age=None, min_interval=0):
        key = hashlib.sha256(json.dumps([method, url, body], sort_keys=True).encode()).hexdigest()
        with self.guard:
            lock = self.locks.setdefault(key, threading.Lock())
        with lock:
            now = time.time()
            cached = self.memory.get(key)
            path = self.cache / (key + '.json') if self.cache else None
            if cached is None and path and path.is_file() and path.stat().st_size <= 32 * 1024 * 1024:
                try:
                    stored = json.loads(path.read_text())
                    # Disk is a disposable success cache, not persistent failure state.
                    if (isinstance(stored, dict) and set(stored) == {'time', 'data'}
                            and type(stored['time']) in (int, float)
                            and math.isfinite(stored['time']) and stored['time'] >= 0):
                        cached = stored
                except (OSError, ValueError, OverflowError):
                    pass
            ttl = self.ttl if max_age is None else min(self.ttl, max_age)
            if cached and 0 <= now - cached.get('time', 0) < ttl:
                self.memory[key] = cached
                if cached.get('error'):
                    raise ValueError('provider request failed earlier in this run')
                return cached['data']
            try:
                host = urlsplit(url).hostname
                self.wait_for_host(host, min_interval)
                deadline = time.monotonic() + 30
                with self.client.stream(method, url, json=body if method == 'POST' else None,
                                        headers={'User-Agent': 'openRuyi-monitor (https://github.com/Jingwiw/openRuyi-monitor)'}) as response:
                    if response.status_code in (429, 503):
                        self.defer_host(host, response.headers.get('Retry-After'))
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
    """Restrict trusted adapters to their declared HTTPS hostnames.

    This checks URL scope, not DNS destinations or arbitrary adapter code;
    adapters are in-process code, not sandboxed plugins.
    """
    def __init__(self, owner, hosts, max_age=None):
        self.owner, self.hosts = owner, hosts
        self.max_age = max_age
        self.today = owner.today

    def json(self, method, url, body=None, *, min_interval=0):
        parsed = urlsplit(url)
        if (method not in ('GET', 'POST') or parsed.scheme != 'https'
                or parsed.hostname not in self.hosts or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment):
            raise ValueError('provider URL outside declared HTTPS hosts')
        return self.owner.json(method, url, body, max_age=self.max_age, min_interval=min_interval)
