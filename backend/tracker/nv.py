from . import version_rules
"""Only the documented nvchecker CLI/JSON interface. Candidate ordering remains native."""
import json
from contextlib import contextmanager
import hashlib
import os
import re
from pathlib import Path
import subprocess
import selectors
import signal
import time
import tempfile
import tomllib
import tomlkit
from .state import success, failure
from .config import public_source, track_fingerprint
from .schedule import Schedule

# The snapshot is tens of MiB: publish the first completion immediately, then
# coalesce bursts instead of rewriting it for every small group of events.
_PUBLISH_INTERVAL_SECONDS = 5.0


def polling(config):
    # The cheap heartbeat selects due tracks; it does not query every provider.
    return Schedule(60, 60, 300)


def refresh(config):
    return Schedule(config['collector'].get('nvchecker_interval_seconds', 21600), 300, 3600)


def due_names(config, snapshot, now):
    options = track_fingerprint(config.get('native_options', {}))
    if snapshot.get('components', {}).get('nvchecker', {}).get('options_fingerprint') != options:
        return list(config['native'])
    policy = refresh(config)
    selected = []
    for name, rule in config['native'].items():
        old = snapshot.get('tracks', {}).get(name, {})
        previous = {**old, 'fingerprint': old.get('configuration_fingerprint'),
                    'status': 'error' if old.get('error') else 'ok'}
        if policy.due(previous, track_fingerprint(rule), now):
            selected.append(name)
    return sorted(selected, key=lambda name: (snapshot.get('tracks', {}).get(name, {}).get('attempted_at') or '', name))


def _event_error(item):
    """Fixed public categories only: provider exception text can contain secrets."""
    detail = str(item.get('error', '')).lower()
    if 'timed out' in detail or 'timeouterror' in detail:
        return 'nvchecker request timeout'
    status = re.search(r'\bhttp ([45][0-9]{2})\b', detail)
    if status:
        return 'nvchecker HTTP ' + status.group(1)
    if 'no version returned' in detail or item.get('event') == 'version string not found.':
        return 'nvchecker no matching version'
    return 'nvchecker reported no usable result'


def import_events(stdout, native, previous, now, command_error=None):
    versions, errors = {}, {}
    malformed = False
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError()
        except (ValueError, TypeError):
            malformed = True
            continue
        name = item.get('name')
        if name not in native:
            continue
        event = item.get('event')
        if item.get('level') == 'error' or event == 'no-result':
            classified = _event_error(item)
            if classified != 'nvchecker reported no usable result' or name not in errors:
                errors[name] = classified
        elif event in ('updated', 'up-to-date'):
            v = item.get('version')
            if isinstance(v, str) and v and len(v) <= 200 and '\n' not in v:
                versions[name] = v
            else:
                errors[name] = 'invalid nvchecker version'
    result = {}
    for name, entry in native.items():
        old = previous.get(name, {})
        fingerprint = track_fingerprint(entry)
        if old and old.get('configuration_fingerprint') != fingerprint:
            # Do not relabel a result from another source/branch after a config edit.
            old = {'previous_configuration': {k: old[k] for k in ('version', 'source', 'fetched_at') if k in old}}
        if name in versions and name not in errors:
            result[name] = success(old, {'version': versions[name], 'source': public_source(entry), 'source_checked_at': None}, now)
        else:
            result[name] = failure(old, errors.get(name) or command_error or 'nvchecker did not report this track', now)
            result[name]['source'] = public_source(entry)
        result[name]['failures'] = old.get('failures', 0) + 1 if result[name].get('error') else 0
        result[name]['configuration_fingerprint'] = fingerprint
        result[name]['reported_at'] = now if name in versions or name in errors else old.get('reported_at')
    component_error = command_error or ('invalid nvchecker JSON log' if malformed else None)
    return result, component_error

def selected_names(config, tracks):
    """Validate before taking locks, invoking the checker, or writing a snapshot."""
    if tracks is None:
        return None
    if not tracks or any(not isinstance(name, str) or not name or name not in config['native'] for name in tracks):
        raise ValueError('tracks must be nonempty configured names')
    return list(dict.fromkeys(tracks))


def dump_config(tables):
    """Serialize native input only when the production TOML reader agrees.

    TOML Kit may support newer syntax than Python's tomllib (also used by
    nvchecker). Validate before creating a candidate or invoking the checker.
    """
    text = tomlkit.dumps(tables)
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ValueError('generated configuration is not supported by the TOML reader') from error
    if not version_rules.same_values(parsed, tables):
        raise ValueError('generated configuration differs from the requested native rules')
    return text


@contextmanager
def command_config(config, names):
    if names is None:
        yield config['nvpath']
        return
    # nvchecker 2.22 --entry accepts just ONE name; it has no --include list.
    # Use its native CLI once with an ephemeral subset, retaining native selection.
    path = Path(config['nvpath'])
    text = path.read_text()
    if version_rules.digest(config['nvpath']) != config['nv_digest']:
        raise ValueError('upstream configuration changed before collection')
    options = dict(tomllib.loads(text).get('__config__', {}))
    if options.get('keyfile'):
        options['keyfile'] = str(path.parent / os.path.expandvars(os.path.expanduser(options['keyfile'])))
    # The tracker owns observation state. A subset must not truncate the native
    # checker's whole-batch version files or suppress unchanged-version JSON events.
    options.pop('oldver', None)
    options.pop('newver', None)
    tables = {'__config__': options, **{name: config['native'][name] for name in names}}
    body = dump_config(tables)
    with tempfile.TemporaryDirectory(prefix='tracker-nv-selected-') as directory:
        selected = Path(directory) / 'nvchecker.toml'
        selected.write_text(body)
        yield str(selected)



def stream_command(command, timeout, native, previous, now, on_results=None):
    """Run the bounded native command, optionally publishing early JSON results.

    One native process/concurrency budget, bounded logs, no extra provider queue.
    Partial publication cannot claim a successful full batch. Final import keeps
    the exact existing failure/timeout semantics, including unreported tracks.
    """
    lines, pending = [], {}
    buffer = b''; total = 0; error = None
    deadline = time.monotonic() + timeout; flushed = time.monotonic() - _PUBLISH_INTERVAL_SECONDS
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    def line(value):
        text = value.decode(errors='replace')
        lines.append(text)
        if on_results is None:
            return
        try:
            event = json.loads(text); name = event.get('name')
            if name in native and event.get('event') in ('updated', 'up-to-date'):
                facts, _ = import_events(text, {name: native[name]}, previous, now)
                if not facts[name].get('error'):
                    pending.update(facts)
        except (ValueError, AttributeError, TypeError):
            pass  # final import reports malformed native output
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    error = 'nvchecker timeout'; break
                for key, _ in selector.select(min(0.5, remaining)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        total += len(chunk); buffer += chunk
                        if total > 16*1024*1024 or len(buffer) > 1024*1024:
                            error = 'nvchecker output limit exceeded'; break
                        while b'\n' in buffer:
                            value, buffer = buffer.split(b'\n', 1); line(value)
                if error:
                    break
                if pending and time.monotonic()-flushed >= _PUBLISH_INTERVAL_SECONDS:
                    on_results(dict(pending)); pending.clear(); flushed = time.monotonic()
        if buffer and not error:
            line(buffer)
        if pending:
            on_results(dict(pending))
        if not error:
            try:
                code = process.wait(timeout=max(0.01, deadline-time.monotonic()))
                error = f'nvchecker exited {code}' if code else None
            except subprocess.TimeoutExpired:
                error = 'nvchecker timeout'
    finally:
        # A timeout or rejected publication must not leave native helper children.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(); process.stdout.close()
    return import_events('\n'.join(lines), native, previous, now, error)


def run(config, previous, now, tracks=None, on_results=None):
    names = selected_names(config, tracks)
    native = config['native'] if names is None else {name: config['native'][name] for name in names}
    if not native:
        return {}, None
    command_names = (sorted(native, key=lambda n: (previous.get(n, {}).get('reported_at', previous.get(n, {}).get('attempted_at')) or '', n))
                     if on_results is not None and names is None else names)
    try:
        with command_config(config, command_names) as path:
            command = ['nvchecker', '--logger=json', '--json-log-fd=1', '--tries', '3', '-c', path]
            return stream_command(command, config['collector'].get('nvchecker_timeout_seconds', 180),
                                  native, previous, now, on_results)
    except OSError:
        return import_events('', native, previous, now, 'nvchecker executable unavailable')
