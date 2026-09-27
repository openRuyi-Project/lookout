"""Shared response limits for collector-owned synchronous HTTP clients."""
import time

import httpx


def read_response(response, *, max_bytes, deadline):
    """Reject late headers, slow bodies and oversized decoded responses.

    Callers start the deadline before opening the stream and configure finite
    HTTPX connect/read/write/pool timeouts. This is a cooperative budget: it
    cannot interrupt an in-flight synchronous read or terminate its thread.
    """
    def check_deadline():
        if time.monotonic() >= deadline:
            raise httpx.ReadTimeout('response exceeds elapsed-time budget', request=response.request)

    check_deadline()
    chunks, size = [], 0
    for chunk in response.iter_bytes():
        check_deadline()
        size += len(chunk)
        if size > max_bytes:
            raise ValueError('response exceeds size limit')
        chunks.append(chunk)
    check_deadline()
    return b''.join(chunks)
