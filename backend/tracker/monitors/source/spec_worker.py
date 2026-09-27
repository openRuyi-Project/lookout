"""One request, one confined RPM process. Not a general command execution service."""
import json
import os
from pathlib import Path
import sys
import threading

import importlib.util

# -I excludes cwd and script directory from sys.path; load this immutable sibling
# explicitly before reading or executing any SPEC bytes.
_module = importlib.util.spec_from_file_location("spec_sandbox", Path(__file__).with_name("spec_sandbox.py"))
_sandbox = importlib.util.module_from_spec(_module)
_module.loader.exec_module(_sandbox)
confine = _sandbox.confine

_LOCK = threading.Lock()


def main():
    inputs, work, result_fd = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
    # Result IPC is a socket, not a pipe (proc-fd reopening of pipes bypasses
    # path-based protection). It is never inherited by exec helpers. Macro stdout
    # is /dev/null and stderr a distinct bounded pipe, never accepted as JSON.
    os.set_inheritable(result_fd, False)
    result = {'values': None, 'error': 'native SPEC sandbox unavailable', 'context': None}
    ready = False
    try:
        import rpm
        sandbox = confine(inputs, work)
        ready = True
        with _LOCK:
            rpm.reloadConfig()
            try:
                rpm.addMacro('_sourcedir', str(work))
                for macro in sorted(inputs.glob('macros.*')):
                    rpm.expandMacro('%%{load:%s}' % macro)
                # RPM's declarative BuildSystem parser creates internal script
                # files using _tmppath, not TMPDIR. Keep those inside this workdir.
                rpm.addMacro('_tmppath', str(work))
                context = {'rpm': rpm.__version__, 'target': rpm.expandMacro('%{_target_cpu}'),
                           'rpm_target': rpm.expandMacro('%{_target}'), 'sandbox': sandbox}
                result['context'] = context
                parsed = rpm.spec(str(inputs / 'package.spec'))
                header = parsed.sourceHeader
                values = {}
                for name, tag in [('name', rpm.RPMTAG_NAME), ('version', rpm.RPMTAG_VERSION),
                                  ('summary', rpm.RPMTAG_SUMMARY), ('license', rpm.RPMTAG_LICENSE),
                                  ('url', rpm.RPMTAG_URL), ('description', rpm.RPMTAG_DESCRIPTION)]:
                    value = header[tag]
                    values[name] = value.decode(errors='replace') if isinstance(value, bytes) else str(value) if value is not None else None
                # One native parse owns both metadata and source identity. No
                # second text/macro interpreter in discovery. Keep Source numbers.
                values['sources'] = [{'number': number, 'url': url}
                                     for url, number, flags in parsed.sources
                                     if flags & rpm.RPMBUILD_ISSOURCE]
                values['go_module'] = (rpm.expandMacro('%{?go_import_path}')
                                       or rpm.expandMacro('%{?goipath}') or None)
                # Read a field from RPM's already-expanded main preamble. A
                # same-named macro or text in %description is not a declaration.
                values['buildsystem'] = None
                for line in parsed.parsed.splitlines():
                    if line.lstrip().startswith('%'):
                        break
                    key, separator, value = line.partition(':')
                    if separator and key.strip().lower() == 'buildsystem':
                        values['buildsystem'] = value.strip() or None
                result.update(values=values, error=None)
            finally:
                rpm.reloadConfig()
    except Exception:
        # Never surface raw stderr, source-controlled exception strings or local paths.
        result['values'] = None
        result['error'] = 'native SPEC parse failed' if ready else 'native SPEC sandbox unavailable'
    encoded = json.dumps(result, ensure_ascii=True).encode()
    while encoded:
        encoded = encoded[os.write(result_fd, encoded):]
    os.close(result_fd)


if __name__ == '__main__':
    main()
