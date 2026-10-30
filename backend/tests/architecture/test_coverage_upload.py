# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
import importlib.util
import io
import ssl
import sys
from http.client import IncompleteRead
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request
from urllib.response import addinfourl

import pytest

spec = importlib.util.spec_from_file_location(
    'coverage_upload', Path(__file__).resolve().parents[3] / 'scripts/coverage-upload.py')
upload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upload)


def http_error(code, headers=None):
    return HTTPError('https://api.example.test/report', code, 'failure',
                     {'X-GitHub-Request-Id': 'ABC:123', **(headers or {})}, io.BytesIO(b'error'))


def response(body=b'{"id": "report"}'):
    return addinfourl(io.BytesIO(body), {}, 'https://api.example.test/report', 201)


@pytest.fixture
def transport(monkeypatch):
    clock = [0]
    sleeps, calls, outcomes = [], [], []

    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    def open_request(req, **kwargs):
        calls.append((req, kwargs))
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(upload.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(upload.time, 'sleep', sleep)
    monkeypatch.setattr(upload.OPENER, 'open', open_request)
    req = Request('https://api.example.test/report', data=b'unchanged-report', method='PUT')
    return SimpleNamespace(req=req, clock=clock, sleeps=sleeps, calls=calls, outcomes=outcomes)


@pytest.mark.parametrize('code', [429, 500, 502, 503, 504])
def test_transient_errors_retry_same_request(transport, code, capsys):
    transport.outcomes.extend([http_error(code), response()])
    with upload.request(transport.req, deadline=220) as result:
        assert result.getcode() == 201
        assert result.read() == b'{"id": "report"}'
    assert transport.sleeps == [10]
    assert len(transport.calls) == 2
    assert all(req is transport.req and options['timeout'] == 20 for req, options in transport.calls)
    assert 'request ID: ABC:123' in capsys.readouterr().out


@pytest.mark.parametrize('code', [301, 302, 400, 401, 403, 404, 422, 501])
def test_permanent_errors_are_not_retried(transport, code):
    transport.outcomes.append(http_error(code))
    with pytest.raises(HTTPError) as caught:
        upload.request(transport.req, deadline=220)
    with caught.value as error:
        assert error.code == code
        assert error.read() == b'error'
    assert len(transport.calls) == 1
    assert not transport.sleeps


def test_retries_stop_after_three_attempts(transport):
    transport.outcomes.extend(http_error(500) for _ in range(3))
    with pytest.raises(HTTPError) as caught:
        upload.request(transport.req, deadline=220)
    caught.value.close()
    assert len(transport.calls) == 3
    assert transport.sleeps == [10, 20]


@pytest.mark.parametrize('error', [URLError('connection failed'), TimeoutError(), ConnectionResetError(), IncompleteRead(b'partial')])
def test_network_failure_retries(transport, error):
    transport.outcomes.extend([error, response()])
    with upload.request(transport.req, deadline=220):
        pass
    assert len(transport.calls) == 2


def test_certificate_error_is_not_retried(transport):
    transport.outcomes.append(URLError(ssl.SSLCertVerificationError('certificate failed')))
    with pytest.raises(URLError):
        upload.request(transport.req, deadline=220)
    assert len(transport.calls) == 1
    assert not transport.sleeps


@pytest.mark.parametrize(('header', 'delay'), [('30', 30), ('invalid', 10),
    ('Thu, 01 Jan 1970 00:00:40 GMT', 40)])
def test_retry_after(transport, monkeypatch, header, delay):
    monkeypatch.setattr(upload.time, 'time', lambda: 0)
    transport.outcomes.extend([http_error(429, {'Retry-After': header}), response()])
    with upload.request(transport.req, deadline=220):
        pass
    assert transport.sleeps == [delay]


def test_retry_after_beyond_budget_does_not_retry_early(transport):
    transport.outcomes.append(http_error(429, {'Retry-After': '3600'}))
    with pytest.raises(HTTPError) as caught:
        upload.request(transport.req, deadline=220)
    caught.value.close()
    assert len(transport.calls) == 1
    assert not transport.sleeps


def test_deadline_bounds_socket_timeout_and_prevents_new_requests(transport):
    transport.outcomes.append(response())
    with upload.request(transport.req, deadline=5):
        pass
    assert transport.calls[0][1]['timeout'] == 5
    transport.clock[0] = 5
    with pytest.raises(TimeoutError):
        upload.request(transport.req, deadline=5)
    assert len(transport.calls) == 1


def test_large_response_is_rejected_and_closed(transport):
    result = response(b'x' * (upload.RESPONSE_LIMIT + 1))
    transport.outcomes.append(result)
    with pytest.raises(ValueError, match='1 MiB'):
        upload.request(transport.req, deadline=220)
    assert result.closed
    assert len(transport.calls) == 1


def test_redirect_and_log_injection_are_rejected(transport, capsys):
    assert upload.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.test') is None
    transport.outcomes.append(http_error(403, {'X-GitHub-Request-Id': 'x\n::error::injected'}))
    with pytest.raises(HTTPError) as caught:
        upload.request(transport.req, deadline=220)
    caught.value.close()
    assert 'injected' not in capsys.readouterr().out


def test_uploader_receives_strict_policy_and_shared_transport(monkeypatch):
    context = SimpleNamespace(should_upload=True, commit_oid='fixture-revision', ref='refs/heads/main', pr_number='')
    monkeypatch.setitem(sys.modules, 'resolve_upload_context', SimpleNamespace(resolve_upload_context=lambda env: context))
    monkeypatch.setenv('FAIL_ON_ERROR', 'false')

    def run(env, opener, status_opener):
        assert env['FAIL_ON_ERROR'] == 'true'
        assert env['COMMIT_OID'] == 'fixture-revision'
        assert opener.func is upload.request
        assert status_opener.func is upload.request
        assert opener.keywords['deadline'] == status_opener.keywords['deadline']
        assert status_opener.keywords['attempts'] == 1
        return 1

    monkeypatch.setitem(sys.modules, 'upload_coverage', SimpleNamespace(main=run))
    assert upload.main() == 1


def test_partial_response_retries_after_closing_connection(transport):
    class Interrupted(io.BytesIO):
        def read(self, size=-1):
            raise IncompleteRead(b'partial')

    broken = addinfourl(Interrupted(), {}, transport.req.full_url, 201)
    transport.outcomes.extend([broken, response()])
    with upload.request(transport.req, deadline=220) as result:
        assert result.getcode() == 201
    assert broken.closed
    assert len(transport.calls) == 2


def test_partial_error_body_keeps_status_and_retries(transport):
    class Interrupted(io.BytesIO):
        def read(self, size=-1):
            raise TimeoutError()

    broken = HTTPError(transport.req.full_url, 500, 'failure', {}, Interrupted())
    transport.outcomes.extend([broken, response()])
    with upload.request(transport.req, deadline=220) as result:
        assert result.getcode() == 201
    assert broken.closed
    assert len(transport.calls) == 2
