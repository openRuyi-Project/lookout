"""Native RPM semantics inside a one-shot, fail-closed Linux sandbox worker."""
import hashlib
import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote
from .state import usable_version

RESOLVER = 7  # native BuildSystem declaration joins the same confined parse
_SIZE_LIMIT = 1024 * 1024
_TIMEOUT = 5.0
_MAX_OUTPUT = 256 * 1024
_MAX_STDERR = 16 * 1024
_WORKERS = threading.BoundedSemaphore(4)


def macro_paths(settings, project, index):
    package = settings.get('spec_macro_package')
    if not package:
        return []
    revision = index.get(package, {}).get('srcmd5')
    if not revision:
        raise ValueError('OBS macro package revision unavailable')
    return [f'/source/{project}/{quote(package, safe="")}/{quote(name, safe="")}?rev={quote(revision, safe="")}&expand=1'
            for name in settings.get('spec_macro_files', [])]


def _kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _run(inputs, work):
    reader, writer = socket.socketpair()
    write_fd = writer.fileno()
    process = None
    try:
        environment = {'PATH': '/usr/bin:/bin', 'HOME': str(work), 'TMPDIR': str(work),
                       'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'TZ': 'UTC',
                       'PYTHONDONTWRITEBYTECODE': '1'}
        process = subprocess.Popen([sys.executable, '-I', str(Path(__file__).with_name('spec_worker.py')),
                                    str(inputs), str(work), str(write_fd)], cwd=work,
                                   env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   pass_fds=(write_fd,), close_fds=True, start_new_session=True)
        writer.close()
        output = bytearray()
        diagnostic_bytes = 0
        deadline = time.monotonic() + _TIMEOUT
        with selectors.DefaultSelector() as selector:
            selector.register(reader, selectors.EVENT_READ, 'result')
            selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, 'native SPEC timeout'
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.data == 'result':
                        output.extend(chunk)
                        if len(output) > _MAX_OUTPUT:
                            return None, 'native SPEC output limit exceeded'
                    else:
                        diagnostic_bytes += len(chunk)
                        if diagnostic_bytes > _MAX_STDERR:
                            return None, 'native SPEC diagnostics limit exceeded'
            try:
                code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                return None, 'native SPEC timeout'
        if code:
            return None, 'native SPEC worker failed'
        try:
            result = json.loads(output)
            if set(result) != {'values', 'error', 'context'}:
                raise ValueError('unexpected result shape')
            if result['error'] not in (None, 'native SPEC parse failed', 'native SPEC sandbox unavailable'):
                raise ValueError('unexpected error')
            context = result['context']
            if context is not None:
                if set(context) != {'rpm', 'target', 'rpm_target', 'sandbox'}:
                    raise ValueError('unexpected context')
                if not all(isinstance(context[k], str) and context[k] and '%' not in context[k]
                           for k in ('rpm', 'target', 'rpm_target')):
                    raise ValueError('invalid target')
                limits = context['sandbox']
                if set(limits) != {'landlock_abi', 'policy', 'seccomp', 'memory_bytes', 'process_limit'}:
                    raise ValueError('unexpected sandbox identity')
                if (type(limits['landlock_abi']) is not int or limits['landlock_abi'] < 6
                        or limits['policy'] != 1 or limits['seccomp'] != 'allow-list'
                        or limits['memory_bytes'] != 256 * 1024 * 1024 or limits['process_limit'] != 256):
                    raise ValueError('unconfined result')
            if result['error'] is None:
                values = result['values']
                if context is None or set(values) != {'name', 'version', 'summary', 'license', 'url', 'description', 'sources', 'go_module', 'buildsystem'}:
                    raise ValueError('unexpected metadata')
                if not all(v is None or isinstance(v, str) for k, v in values.items() if k != 'sources'):
                    raise ValueError('non-string metadata')
                if (not isinstance(values['sources'], list) or len(values['sources']) > 4096
                        or any(not isinstance(v, dict) or set(v) != {'number', 'url'}
                               or type(v['number']) is not int or v['number'] < 0
                               or not isinstance(v['url'], str) for v in values['sources'])):
                    raise ValueError('invalid source identity')
            elif result['values'] is not None:
                raise ValueError('values returned by failed worker')
            return result, result['error']
        except (ValueError, TypeError, KeyError):
            return None, 'native SPEC invalid worker result'
    except (OSError, ValueError):
        return None, 'native SPEC sandbox unavailable'
    finally:
        if process is not None:
            # Remove descendants even if the direct worker already exited.
            _kill_group(process)
            process.stderr.close()
        reader.close()
        writer.close()


def _parse(spec, macros):
    if len(spec) > _SIZE_LIMIT:
        raise ValueError('SPEC exceeds size limit')
    if len(macros) > 16:
        raise ValueError('too many native macro files')
    for provenance, data in macros:
        if len(data) > _SIZE_LIMIT or hashlib.sha256(data).hexdigest() != provenance['sha256']:
            raise ValueError('native macro content does not match its pinned hash')
    context = {'resolver': RESOLVER, 'rpm': None, 'target': None,
               'environment': 'one-shot Landlock+seccomp native RPM worker',
               'additional_macros': [m[0] for m in macros]}
    with _WORKERS, TemporaryDirectory(prefix='openruyi-rpmspec-') as temp:
        inputs, work = Path(temp) / 'inputs', Path(temp) / 'work'
        inputs.mkdir(); work.mkdir()
        (inputs / 'package.spec').write_bytes(spec)
        for index, (_, data) in enumerate(macros):
            (inputs / f'macros.{index:02}').write_bytes(data)
        result, error = _run(inputs, work)
        if result and isinstance(result.get('context'), dict):
            context.update(result['context'])
        return (result.get('values') if result else None), error, context


def query(spec, macros=()):
    values, error, context = _parse(spec, macros)
    value = values.get('version') if values else None
    valid = bool(value) and usable_version(value)
    return {'version': value if valid else None,
            'native_query': {'context': context, 'spec_sha256': hashlib.sha256(spec).hexdigest(), 'error': error},
            'version_error': None if valid else (error or 'native rpmspec could not establish VERSION')}


def describe(spec, macros=()):
    value, error, context = _parse(spec, macros)
    metadata = None
    if value and value['name']:
        metadata = {name: value[name] or None for name in ('name', 'version', 'summary', 'license', 'url')}
        metadata['description'] = (value['description'] or '').strip() or None
        metadata['sources'] = value['sources']
        metadata['go_module'] = value['go_module']
        metadata['buildsystem'] = value['buildsystem']
    return {'metadata': metadata,
            'native_query': {'context': context, 'spec_sha256': hashlib.sha256(spec).hexdigest(), 'error': error},
            'metadata_error': None if metadata else (error or 'native rpmspec could not parse SPEC metadata')}


def resolve(client, project, name, fact, macro_files=()):
    filename = fact.get('filename')
    if not filename or '/' in filename or not filename.endswith('.spec'):
        return fact
    # The content-addressed OBS revision is fixed: never parse a newer SPEC for an older row.
    path = f'/source/{project}/{quote(name, safe="")}/{quote(filename, safe="")}?rev={quote(fact["srcmd5"], safe="")}&expand=1'
    macros = []
    for macro_path in macro_files:
        if not macro_path.startswith('/source/') or '?rev=' not in macro_path:
            raise ValueError('native macros require a pinned OBS source path')
        data = client.get(macro_path)
        macros.append(({'path': macro_path, 'sha256': hashlib.sha256(data).hexdigest()}, data))
    result = query(client.get(path), macros=macros)
    return {**fact, **result, 'native_macro_paths': list(macro_files),
            'version_basis': 'rpmspec VERSION on pinned OBS SPEC (see native_query.context)'}
