"""Serial, bounded GitHub requests; credentials never enter persisted facts."""
import json
import os
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from tracker.providers.client import IO
from tracker.providers.http import USER_AGENT, read_response


class BudgetExhausted(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, retry_at):
        super().__init__('GitHub requested a cooldown')
        self.retry_at = retry_at


class Client:
    def __init__(self, budget, *, io=None):
        self.io = io or IO(workers=1)
        self.remaining = budget
        self.retry_at = None
        self.token = os.environ.get('LOOKOUT_GITHUB_TOKEN', '')
        self.interval_floor = 0 if self.token else 600
        if not self.token:
            self.remaining = min(self.remaining, 6)

    def close(self):
        self.io.close()

    def get(self, path, *, etag=None):
        if self.retry_at:
            raise RateLimited(self.retry_at)
        if self.remaining <= 0:
            raise BudgetExhausted
        self.remaining -= 1
        headers = {'User-Agent': USER_AGENT, 'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        if etag:
            headers['If-None-Match'] = etag
        self.io.wait_for_host('api.github.com', 1)
        with self.io.connection() as client, client.stream('GET', 'https://api.github.com' + path,
                                                          headers=headers) as response:
            if response.status_code == 429 or response.status_code == 403 and (
                    response.headers.get('Retry-After') or response.headers.get('X-RateLimit-Remaining') == '0'):
                now = time.time()
                try:
                    raw = response.headers.get('Retry-After')
                    retry = now + float(raw) if raw else float(response.headers.get('X-RateLimit-Reset', now + 300))
                except ValueError:
                    try:
                        retry = parsedate_to_datetime(response.headers['Retry-After']).timestamp()
                    except (ValueError, KeyError):
                        retry = now + 300
                self.retry_at = datetime.fromtimestamp(max(now + 60, retry), UTC).isoformat()
                raise RateLimited(self.retry_at)
            remaining = response.headers.get('X-RateLimit-Remaining')
            reset = response.headers.get('X-RateLimit-Reset')
            reserve = 50 if self.token else 6
            if remaining and reset and remaining.isdigit() and reset.isdigit() and int(remaining) <= reserve:
                self.retry_at = datetime.fromtimestamp(max(time.time() + 60, int(reset)), UTC).isoformat()
            if response.status_code == 304:
                return None, etag
            response.raise_for_status()
            body = read_response(response, max_bytes=16 * 1024 * 1024, deadline=time.monotonic() + 30)
            return json.loads(body), response.headers.get('ETag')
