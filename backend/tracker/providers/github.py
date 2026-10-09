"""Serial, bounded GitHub requests; credentials never enter persisted facts."""
import json
import math
import os
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from tracker.providers.client import IO
from tracker.providers.http import USER_AGENT, read_response


class AuthenticationRejected(Exception):
    pass


class BudgetExhausted(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, retry_at, *, rejected=True):
        super().__init__('GitHub requested a cooldown')
        self.retry_at = retry_at
        self.rejected = rejected


def retry_at(headers, now):
    """Normalize provider cooldowns without letting malformed dates discard facts."""
    try:
        raw = headers.get('Retry-After')
        if raw:
            try:
                retry = now + float(raw)
            except ValueError:
                retry = parsedate_to_datetime(raw).timestamp()
        else:
            retry = float(headers.get('X-RateLimit-Reset', now + 300))
        if not math.isfinite(retry):
            raise ValueError('non-finite cooldown')
        return datetime.fromtimestamp(max(now + 60, retry), UTC).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return datetime.fromtimestamp(now + 300, UTC).isoformat()


class Client:
    def __init__(self, budget, *, io=None, auth_retry_at=0):
        self.io = io or IO(workers=1)
        self.remaining = budget
        self.retry_at = None
        self.rejected = False
        self.token = os.environ.get('LOOKOUT_GITHUB_TOKEN', '')
        self.auth_retry_at = auth_retry_at
        if self.auth_retry_at > time.time():
            self.token = ''
        self.interval_floor = 0 if self.token else 600
        self.deadline = time.monotonic() + 120
        if not self.token:
            self.remaining = min(self.remaining, 60)

    def close(self):
        self.io.close()

    def get(self, path, *, etag=None, auth_only=False):
        for _ in range(2):
            if self.retry_at:
                raise RateLimited(self.retry_at, rejected=self.rejected)
            if self.remaining <= 0 or time.monotonic() >= self.deadline:
                raise BudgetExhausted
            self.remaining -= 1
            headers = {'User-Agent': USER_AGENT, 'Accept': 'application/vnd.github+json',
                       'X-GitHub-Api-Version': '2022-11-28'}
            if self.token:
                headers['Authorization'] = 'Bearer ' + self.token
            if etag:
                headers['If-None-Match'] = etag
            self.io.wait_for_host('api.github.com', 1)
            deadline = time.monotonic() + 30
            with self.io.connection() as client, client.stream('GET', 'https://api.github.com' + path,
                                                              headers=headers) as response:
                if response.status_code == 429 or response.status_code == 403 and (
                        response.headers.get('Retry-After') or response.headers.get('X-RateLimit-Remaining') == '0'):
                    self.retry_at = retry_at(response.headers, time.time())
                    self.rejected = True
                    raise RateLimited(self.retry_at)
                permission_denied = False
                if self.token and response.status_code == 403:
                    body = read_response(response, max_bytes=65536, deadline=deadline)
                    try:
                        message = json.loads(body).get('message', '')
                    except (ValueError, AttributeError):
                        message = ''
                    permission_denied = message in (
                        'Resource not accessible by personal access token',
                        'Resource not accessible by integration')
                if self.token and (response.status_code == 401 or permission_denied):
                    # Release the connection before retrying the public endpoint.
                    self.token = ''
                    self.auth_retry_at = time.time() + 3600
                    self.interval_floor = 600
                    self.remaining = min(self.remaining, 60)
                    if auth_only:
                        raise AuthenticationRejected
                    continue
                try:
                    remaining = int(response.headers.get('X-RateLimit-Remaining', ''))
                except ValueError:
                    remaining = -1
                reserve = 50 if self.token else 2
                if 0 <= remaining <= reserve and response.headers.get('X-RateLimit-Reset'):
                    self.retry_at = retry_at(response.headers, time.time())
                if self.token and (response.status_code == 304 or 200 <= response.status_code < 300):
                    self.auth_retry_at = 0
                if response.status_code == 304:
                    return None, etag
                response.raise_for_status()
                body = read_response(response, max_bytes=16 * 1024 * 1024, deadline=deadline)
                return json.loads(body), response.headers.get('ETag')
        raise BudgetExhausted
