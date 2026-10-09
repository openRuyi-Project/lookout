"""Discover tested GitHub publications without polling the image registry."""
import json
import math
import os
import re
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from credentials import github_token


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


urlopen = build_opener(NoRedirect()).open


_MAX_RESPONSE = 1024 * 1024


def channel_repository(reference):
    match = re.fullmatch(r'ghcr\.io/([a-z0-9_.-]+/[a-z0-9_.-]+):(main|latest)', reference)
    return match[1] if match else None


class Deferred(RuntimeError):
    def __init__(self, reason, retry_at):
        super().__init__(reason + '; service unchanged')
        self.reason, self.retry_at = reason, retry_at


def save_state(path, value):
    import tempfile

    from deployment import publish_file
    path = Path(path)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        staged = Path(stream.name)
        stream.write(json.dumps(value).encode())
    try:
        publish_file(staged, path, replace=True)
    finally:
        staged.unlink(missing_ok=True)


def cooldown(headers, now):
    try:
        raw = headers.get('Retry-After')
        if raw:
            try:
                value = now + float(raw)
            except ValueError:
                value = parsedate_to_datetime(raw).timestamp()
        else:
            value = float(headers.get('X-RateLimit-Reset', now + 300))
        if math.isfinite(value):
            return max(now + 60, value)
    except (ValueError, TypeError, OverflowError):
        # Malformed rate-limit headers use the bounded fallback below.
        pass
    return now + 300


def published_image(reference, current_image, current_revision, workflow, *, state_path=None, credential_file=None):
    repository = channel_repository(reference)
    if not repository or not re.fullmatch(r'[A-Za-z0-9_.-]+\.ya?ml', workflow):
        raise ValueError('publication checks require a GHCR main/latest channel and a workflow filename')
    if not re.fullmatch(r'[0-9a-f]{40}', current_revision or ''):
        raise ValueError('running image must declare its source revision')
    now = time.time()
    key = reference + '/' + workflow
    previous = {}
    if state_path and Path(state_path).exists():
        with Path(state_path).open('rb') as stream:
            cached = stream.read(2 * _MAX_RESPONSE + 1)
        if len(cached) > 2 * _MAX_RESPONSE:
            raise ValueError('publication checkpoint exceeds the size budget')
        previous = json.loads(cached)
        if previous.get('key') != key:
            previous = {}
    token = github_token(credential_file) if credential_file else os.environ.get('LOOKOUT_GITHUB_TOKEN')
    auth_retry_at = previous.get('auth_retry_at', 0)
    auth_probe = bool(token and (auth_retry_at and auth_retry_at <= now
                                or not auth_retry_at and previous.get('next_attempt_at', 0) > now
                                and previous.get('authenticated') is not True))
    if previous.get('next_attempt_at', 0) > now and not auth_probe:
        raise Deferred(previous['reason'], previous['next_attempt_at'])
    if auth_retry_at > now:
        token = None

    def save_checkpoint(value):
        save_state(state_path, {'authenticated': bool(token), **value, 'auth_retry_at': auth_retry_at})

    query = urlencode(dict(branch='main', status='success', per_page=1))
    url = f'https://api.github.com/repos/{repository}/actions/workflows/{quote(workflow, safe="")}/runs?{query}'
    request = Request(url, headers={'Accept': 'application/vnd.github+json',
                                  'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'Lookout-updater'})
    if token:
        request.add_header('Authorization', 'Bearer ' + token)
    if previous.get('etag') and previous.get('body'):
        request.add_header('If-None-Match', previous['etag'])
    etag = None
    for _ in range(2):
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read(_MAX_RESPONSE + 1)
                etag = getattr(response, 'headers', {}).get('ETag')
        except HTTPError as error:
            if error.code == 304 and previous.get('body'):
                body = previous['body'].encode()
                etag = previous.get('etag')
            else:
                limited = error.code == 429 or error.code == 403 and (
                    error.headers.get('Retry-After') or error.headers.get('X-RateLimit-Remaining') == '0')
                permission_denied = False
                if token and error.code == 403 and not limited:
                    try:
                        message = json.loads(error.read(65536)).get('message', '')
                    except (ValueError, AttributeError):
                        message = ''
                    permission_denied = message in (
                        'Resource not accessible by personal access token',
                        'Resource not accessible by integration')
                if token and (error.code == 401 or permission_denied):
                    auth_retry_at = now + 3600
                    token = None
                    request.remove_header('Authorization')
                    if state_path:
                        save_checkpoint({**previous, 'key': key, 'checked_at': now, 'authenticated': False})
                    error.close()
                    if previous.get('next_attempt_at', 0) > now:
                        raise Deferred(previous['reason'], previous['next_attempt_at']) from None
                    auth_probe = False
                    continue
                if auth_probe:
                    auth_retry_at = max(now + 3600, cooldown(error.headers, now))
                    if state_path:
                        save_checkpoint({**previous, 'key': key, 'authenticated': False})
                    raise Deferred('GitHub authentication probe deferred', auth_retry_at) from None
                reason = 'GitHub rate limit' if limited else ('GitHub authentication failed' if error.code == 401 else 'GitHub publication access failed')
                retry = cooldown(error.headers, now) if limited else now + (3600 if error.code in (401, 403) else 300)
                if state_path:
                    save_checkpoint({'key': key, 'reason': reason, 'next_attempt_at': retry})
                raise Deferred(reason, retry) from None
        except (URLError, TimeoutError) as error:
            if auth_probe and state_path:
                auth_retry_at = now + 3600
                save_checkpoint({**previous, 'key': key, 'authenticated': False})
            # Discovery failure must never turn into a speculative registry pull.
            raise RuntimeError('GitHub publication check failed; service unchanged') from error
        break
    if token:
        auth_retry_at = 0
    if len(body) > _MAX_RESPONSE:
        raise ValueError('GitHub publication response exceeds the size budget')
    runs = json.loads(body)['workflow_runs']
    if not isinstance(runs, list):
        raise ValueError('GitHub publication response must contain a run list')
    if not runs:
        if state_path:
            save_checkpoint({'key': key, 'checked_at': now})
        return current_image, current_revision
    run = runs[0]
    revision = run.get('head_sha')
    if (run.get('status') != 'completed' or run.get('conclusion') != 'success'
            or run.get('head_branch') != 'main' or run.get('event') not in ('push', 'workflow_dispatch')
            or run.get('head_repository', {}).get('full_name', '').lower() != repository
            or not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision)):
        raise ValueError('GitHub run does not identify a successful channel publication')
    if state_path:
        cached_run = {key: run[key] for key in ('status', 'conclusion', 'head_branch', 'event', 'head_sha')}
        cached_run['head_repository'] = {'full_name': repository}
        save_checkpoint({'key': key, 'checked_at': now, 'etag': etag,
                               'body': json.dumps({'workflow_runs': [cached_run]})})
    return (current_image if revision == current_revision else f'ghcr.io/{repository}:sha-{revision}'), revision
