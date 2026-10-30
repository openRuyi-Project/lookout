#!/usr/bin/env python3
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""Run the pinned GitHub uploader with bounded, retryable HTTP transport."""
import functools
import io
import os
import re
import ssl
import sys
import time
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, build_opener
from urllib.response import addinfourl

RETRYABLE = {429, 500, 502, 503, 504}
RESPONSE_LIMIT = 1024 * 1024
REQUEST_TIMEOUT = 20


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Coverage credentials must not follow an API redirect.
        return None


OPENER = build_opener(NoRedirect)


def retry_delay(headers, fallback):
    value = headers.get('Retry-After', '')
    if value.isdecimal():
        return int(value)
    if value:
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return fallback
    return fallback


def read_response(response):
    body = response.read(RESPONSE_LIMIT + 1)
    if len(body) > RESPONSE_LIMIT:
        raise ValueError('Coverage API response exceeds 1 MiB')
    return body


def request(req, timeout=REQUEST_TIMEOUT, *, deadline, attempts=3):
    """Retry the same request; never retry authentication or validation errors."""
    for attempt in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Coverage upload time budget exhausted')
        delay = 10 * (2 ** attempt)
        try:
            with OPENER.open(req, timeout=min(timeout, REQUEST_TIMEOUT, remaining)) as response:
                body = read_response(response)
                return addinfourl(io.BytesIO(body), response.headers, response.url, response.status)
        except HTTPError as error:
            with error:
                try:
                    body = read_response(error)
                except (OSError, IncompleteRead):
                    body = b''
            request_id = error.headers.get('X-GitHub-Request-Id', '')
            if re.fullmatch(r'[A-Za-z0-9:-]{1,128}', request_id) is None:
                request_id = 'unavailable'
            print(f'Coverage HTTP {error.code}; request ID: {request_id}')
            failure = HTTPError(req.full_url, error.code, error.reason, error.headers, io.BytesIO(body))
            if error.code not in RETRYABLE:
                raise failure from None
            delay = max(delay, retry_delay(error.headers, delay))
        except (URLError, TimeoutError, ConnectionError, IncompleteRead) as error:
            if isinstance(error, URLError) and isinstance(error.reason, ssl.SSLCertVerificationError):
                raise
            failure = error
        if attempt + 1 == attempts or delay >= deadline - time.monotonic():
            raise failure
        if isinstance(failure, HTTPError):
            failure.close()
        print(f'Coverage request retry {attempt + 2}/{attempts} in {delay:g}s')
        time.sleep(delay)
    raise AssertionError('No coverage request attempted')


def main():
    # PYTHONPATH selects the upstream revision pinned by the workflow.
    from resolve_upload_context import resolve_upload_context
    from upload_coverage import main as upload

    env = dict(os.environ)
    context = resolve_upload_context(env)
    if not context.should_upload:
        print(f'::{context.annotation_level}::{context.message}')
        return int(context.annotation_level == 'error')
    env.update(COMMIT_OID=context.commit_oid, REF=context.ref, PR_NUMBER=context.pr_number,
               FAIL_ON_ERROR='true', WAIT_FOR_PROCESSING_TIMEOUT='160')
    transport = functools.partial(request, deadline=time.monotonic() + 220)
    try:
        return upload(env, opener=transport, status_opener=functools.partial(transport, attempts=1))
    except (OSError, ValueError):
        print('::error::Coverage request failed or exceeded its time or size budget.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
