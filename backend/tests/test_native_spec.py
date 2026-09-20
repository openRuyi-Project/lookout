# SPDX-FileCopyrightText: (C) 2026 Institute of Software, Chinese Academy of Sciences (ISCAS)
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""Real librpm semantics and fail-closed worker confinement.

Run native tests with the production non-privileged container flags; never relax
seccomp or mount production data to make a security fixture pass.
"""
from tracker import native_spec
import hashlib
import pytest

SPEC=b'''%global commit f60e50e887d3c49e91ac9b06d8199b36152632fa
%global shortcommit %(c=%{commit}; echo ${c:0:7})
Name: sample
Version: 0+git20260202.%{shortcommit}
Release: 1
Summary: Fixture
License: MIT
%description
Fixture
'''

def test_real_rpmspec_macro_query():
    result=native_spec.query(SPEC)
    assert result['version']=='0+git20260202.f60e50e'
    assert result['native_query']['error'] is None
    assert result['native_query']['context']['resolver']==native_spec.RESOLVER

def test_unknown_macro_stays_unknown():
    result=native_spec.query(SPEC.replace(b'%{shortcommit}',b'%{missing_version_macro}'))
    assert result['version'] is None and result['version_error']

def test_pinned_obs_spec_request():
    class C:
        def get(self,path):
            assert path=='/source/openruyi/foo/_service%3Aobs_scm%3Afoo.spec?rev=abc123&expand=1'
            return SPEC
    result=native_spec.resolve(C(),'openruyi','foo',{'version':None,'raw_version':'0+git20260202.MACRO','srcmd5':'abc123','filename':'_service:obs_scm:foo.spec'})
    assert result['version']=='0+git20260202.f60e50e' and result['raw_version'].endswith('MACRO')

def test_extra_macro_is_native_and_hash_checked():
    macro=b'%fixture_version 3.10.0\n'
    provenance={'path':'/source/fixture?rev=abc','sha256':hashlib.sha256(macro).hexdigest()}
    spec=SPEC.replace(b'0+git20260202.%{shortcommit}',b'%{fixture_version}')
    result=native_spec.query(spec,macros=[(provenance,macro)])
    assert result['version']=='3.10.0'
    assert result['native_query']['context']['additional_macros']==[provenance]
    with pytest.raises(ValueError,match='pinned hash'):
        native_spec.query(spec,macros=[(provenance,macro+b'bad')])

def test_macro_revision_follows_obs_not_a_literal_version():
    settings={'spec_macro_package':'rpm-config', 'spec_macro_files':['macros.buildsystem']}
    first=native_spec.macro_paths(settings,'openruyi',{'rpm-config':{'srcmd5':'first'}})
    next_=native_spec.macro_paths(settings,'openruyi',{'rpm-config':{'srcmd5':'next'}})
    assert first==['/source/openruyi/rpm-config/macros.buildsystem?rev=first&expand=1']
    assert next_==['/source/openruyi/rpm-config/macros.buildsystem?rev=next&expand=1']
    with pytest.raises(ValueError,match='revision unavailable'):
        native_spec.macro_paths(settings,'openruyi',{})

def test_oversize_spec_rejected():
    with pytest.raises(ValueError,match='size limit'):
        native_spec.query(b'Name: x\n'+b'#'*(1024*1024+1))

def test_describe_extracts_static_metadata():
    result=native_spec.describe(SPEC)
    md=result['metadata']
    assert md is not None and result['metadata_error'] is None
    assert md['name']=='sample' and md['summary']=='Fixture' and md['license']=='MIT'
    assert md['description']=='Fixture'
    assert result['native_query']['error'] is None

def test_describe_preserves_multiline_description():
    body=SPEC.replace(b'%description\nFixture\n', b'%description\nline one\nline two\nline three\n')
    md=native_spec.describe(body)['metadata']
    assert md['description']=='line one\nline two\nline three'

def test_describe_unparseable_spec_is_error_not_guess():
    result=native_spec.describe(b'this is not a spec at all\n')
    assert result['metadata'] is None and result['metadata_error']

def test_concurrent_parsing_does_not_corrupt_macro_state():
    # librpm macro state is global; the module lock must keep parallel callers correct.
    import concurrent.futures
    m1=b'%fixture_version 1.0.0\n'; m2=b'%fixture_version 2.0.0\n'
    def run(macro,expect):
        prov={'path':'/x','sha256':hashlib.sha256(macro).hexdigest()}
        spec=SPEC.replace(b'0+git20260202.%{shortcommit}',b'%{fixture_version}')
        return native_spec.query(spec,macros=[(prov,macro)])['version']==expect
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        tasks=[ex.submit(run,m1,'1.0.0') for _ in range(20)]+[ex.submit(run,m2,'2.0.0') for _ in range(20)]
        assert all(t.result() for t in tasks)


def expression(text):
    return SPEC.replace(b'0+git20260202.%{shortcommit}', text.encode())


def python_expression(code):
    import shlex
    return expression('%(python3 -c ' + shlex.quote(code) + ')')


def test_context_reports_actual_rpm_target_without_changing_version():
    result = native_spec.query(expression('1.%{_target_cpu}'))
    context = result['native_query']['context']
    assert result['version'] == '1.' + context['target']
    assert context['resolver'] == native_spec.RESOLVER and context['sandbox']['landlock_abi'] >= 6
    assert context['sandbox']['seccomp'] == 'allow-list'


def test_shell_pipeline_remains_native():
    result = native_spec.query(expression("%(printf '1.2.3\\n' | cut -d. -f1,2)"))
    assert result['version'] == '1.2'


def test_spec_cannot_read_or_overwrite_external_file(tmp_path):
    secret = tmp_path / 'operator-secret'
    secret.write_text('fixture-private')
    for mode in ('r', 'w'):
        body = "%{lua: local f=io.open('" + str(secret) + "','" + mode + "'); if f then f:close(); print('escaped') else print('blocked') end}"
        result = native_spec.query(expression(body))
        assert result['version'] == 'blocked', result
        assert secret.read_text() == 'fixture-private'


def test_spec_cannot_read_parent_environment(monkeypatch):
    monkeypatch.setenv('TRACKER_FIXTURE_TOKEN', 'private-canary')
    result = native_spec.query(expression("%{lua: print(os.getenv('TRACKER_FIXTURE_TOKEN') and 'leaked' or 'clear')}"))
    assert result['version'] == 'clear'


def test_spec_helpers_do_not_inherit_parent_or_result_fds(tmp_path):
    import os
    secret = tmp_path / 'fd-canary'; secret.write_text('private-canary')
    with secret.open() as stream:
        fd = stream.fileno(); os.set_inheritable(fd, True)
        result = native_spec.query(python_expression(f"import os\ntry: os.read({fd},100); print('leaked')\nexcept OSError: print('closed')"))
    assert result['version'] == 'closed', result


@pytest.mark.parametrize('family', ['AF_INET', 'AF_INET6', 'AF_UNIX'])
def test_all_socket_families_denied(family):
    result = native_spec.query(python_expression(f"import socket\ntry: socket.socket(socket.{family}); print('escaped')\nexcept PermissionError: print('blocked')"))
    assert result['version'] == 'blocked', result


@pytest.mark.parametrize('call', [
    "libc.ptrace(0, 0, 0, 0)",
    "libc.process_vm_readv(os.getppid(), None, 0, None, 0, 0)",
    "libc.process_vm_writev(os.getppid(), None, 0, None, 0, 0)",
    "libc.kill(os.getppid(), 0)",  # signal 0 cannot harm the parent even on failure
])
def test_process_interference_syscalls_denied(call):
    code = "import ctypes,errno,os\nlibc=ctypes.CDLL(None,use_errno=True)\nvalue=" + call + "\nprint('blocked' if value == -1 and ctypes.get_errno() == errno.EPERM else 'escaped')"
    result = native_spec.query(python_expression(code))
    assert result['version'] == 'blocked', result


def test_worker_wall_timeout_and_next_parse(monkeypatch):
    import time
    monkeypatch.setattr(native_spec, '_TIMEOUT', 0.25)
    started = time.monotonic()
    result = native_spec.query(expression('%(sleep 20; echo 1.0)'))
    assert result['version'] is None and result['version_error'] == 'native SPEC timeout'
    assert time.monotonic() - started < 2
    monkeypatch.setattr(native_spec, '_TIMEOUT', 5)
    assert native_spec.query(SPEC)['version'] == '0+git20260202.f60e50e'


def test_metadata_result_output_is_bounded():
    body = SPEC.replace(b'Summary: Fixture', b'Summary: %{lua: print(string.rep("a",300000))}')
    result = native_spec.describe(body)
    assert result['metadata'] is None
    assert result['metadata_error'] == 'native SPEC output limit exceeded'


def test_macro_stderr_is_bounded():
    result = native_spec.query(expression('%(python3 -c \'import sys;sys.stderr.write("a"*20000);print("1.0")\')'))
    assert result['version'] is None and result['version_error'] == 'native SPEC diagnostics limit exceeded'


def test_macro_stdout_cannot_forge_worker_result():
    prefix = b'%{lua: io.stdout:write(\'{"values":{"version":"forged"},"error":null}\\n\')}\n'
    result = native_spec.query(prefix + SPEC)
    assert result['version'] == '0+git20260202.f60e50e', result


def test_worker_failure_is_closed_not_in_process_fallback(monkeypatch, tmp_path):
    import subprocess
    def failed(*args, **kwargs):
        raise FileNotFoundError('fixture worker unavailable')
    monkeypatch.setattr(subprocess, 'Popen', failed)
    marker = tmp_path / 'must-not-be-written'
    result = native_spec.query(expression(f'%(echo unsafe > {marker}; echo 1.0)'))
    assert result['version'] is None and 'sandbox unavailable' in result['version_error']
    assert not marker.exists()


def test_uid_process_limit_actually_stops_fork_and_cannot_be_raised():
    # Pipe EOF releases every harmless child without relying on blocked kill().
    # The bound counts the real UID's existing processes, not just this worker.
    code = '''import os,errno,resource
r,w=os.pipe(); children=[]; bounded=False
try:
 for i in range(300):
  try: pid=os.fork()
  except OSError as e:
   bounded=e.errno==errno.EAGAIN; break
  if pid==0:
   os.close(w); os.read(r,1); os._exit(0)
  children.append(pid)
finally:
 os.close(w); os.close(r)
 for pid in children: os.waitpid(pid,0)
try:
 resource.setrlimit(resource.RLIMIT_NPROC,(10000,10000)); raised=True
except (OSError,ValueError): raised=False
print(('bounded.' if bounded and not raised else 'escaped.')+str(len(children)))'''
    result = native_spec.query(python_expression(code))
    assert result['version'] and result['version'].startswith('bounded.'), result
    assert int(result['version'].split('.')[1]) < 256
    print('NPROC_OBSERVED', result['version'])


def test_unsupported_landlock_does_not_degrade_to_unconfined(monkeypatch, tmp_path):
    from tracker import spec_sandbox
    from unittest.mock import Mock
    fake = Mock(); fake.syscall.return_value = 5
    monkeypatch.setattr(spec_sandbox.ctypes, 'CDLL', lambda *a, **k: fake)
    with pytest.raises(spec_sandbox.SandboxUnavailable, match='ABI 6'):
        spec_sandbox._landlock(tmp_path, tmp_path)


def test_memory_limit_is_effective():
    code = "import resource\ntry: x=bytearray(512*1024*1024); print('escaped')\nexcept MemoryError: print('bounded')"
    result = native_spec.query(python_expression(code))
    assert result['version'] == 'bounded', result


def test_lua_cannot_reopen_private_ipc_through_proc_fd():
    body = "%{lua: local leaked=false; for n=3,31 do local f=io.open('/proc/self/fd/'..n,'w'); if f then f:close(); leaked=true end end; print(leaked and 'escaped' or 'blocked')}"
    result = native_spec.query(expression(body))
    assert result['version'] == 'blocked', result


def test_rpm_lua_surface_has_no_raw_fd_output_api():
    # The pinned RPM Lua API exposes path/stream I/O, not a raw fd write primitive.
    # A runtime upgrade adding one requires reviewing the private IPC boundary.
    summary = b'%{lua: local t={}; for _,n in ipairs({"posix","io","rpm"}) do for k,v in pairs(_G[n]) do if type(v)=="function" then table.insert(t,n.."."..k) end end end; table.sort(t); print(table.concat(t,","))}'
    result = native_spec.describe(SPEC.replace(b'Summary: Fixture', b'Summary: ' + summary))
    assert result['metadata'], result
    apis = set(result['metadata']['summary'].split(','))
    assert not apis.intersection({'posix.write', 'posix.pwrite', 'posix.fdopen', 'posix.dup', 'posix.dup2', 'posix.send', 'io.fdopen'})
    print('RPM_LUA_APIS', ','.join(sorted(apis)))


def test_declarative_buildsystem_temporary_scripts_stay_in_private_work():
    macro = b'%buildsystem_fixture_conf() %nil\n%buildsystem_fixture_build() %nil\n%buildsystem_fixture_install() %nil\n'
    provenance = {'path': 'fixture/macros.buildsystem', 'sha256': hashlib.sha256(macro).hexdigest()}
    spec = SPEC.replace(b'Release: 1\n', b'Release: 1\nSource0: fixture.tar.gz\nBuildSystem: fixture\n')
    result = native_spec.describe(spec, macros=[(provenance, macro)])
    assert result['metadata'] and result['metadata']['version'] == '0+git20260202.f60e50e', result
    assert result['native_query']['context']['additional_macros'] == [provenance]


def test_spec_cannot_redirect_rpm_buildsystem_tempfiles_outside_work(tmp_path):
    macro = b'%buildsystem_fixture_conf() %nil\n%buildsystem_fixture_build() %nil\n%buildsystem_fixture_install() %nil\n'
    provenance = {'path': 'fixture/macros.buildsystem', 'sha256': hashlib.sha256(macro).hexdigest()}
    spec = (f'%global _tmppath {tmp_path}\n'.encode() +
            SPEC.replace(b'Release: 1\n', b'Release: 1\nSource0: fixture.tar.gz\nBuildSystem: fixture\n'))
    result = native_spec.describe(spec, macros=[(provenance, macro)])
    assert result['metadata'] is None and result['metadata_error'] == 'native SPEC parse failed'
    assert not list(tmp_path.iterdir())


def test_native_source_and_module_identity_share_parse_hash_and_context():
    from tracker import discover_sources
    text=b'''%global owner Team
%global goipath github.com/%{owner}/Widget/v2
Name: widget
Version: 1.2.3
Release: 1
Summary: Fixture
License: MIT
URL: https://widget.example.org/
Source0: https://github.com/%{owner}/Widget/archive/v%{version}.tar.gz
%ifarch riscv64
Patch2000: arch-only.patch
%endif
Source1: auxiliary.dat
%description
Fixture
'''
    result=native_spec.describe(text)
    assert result['metadata_error'] is None
    md=result['metadata'];assert md['go_module']=='github.com/Team/Widget/v2'
    assert md['sources']==[{'number':0,'url':'https://github.com/Team/Widget/archive/v1.2.3.tar.gz'},{'number':1,'url':'auxiliary.dat'}]
    found=discover_sources.hints({'name':'widget','current':'1.2.3','homepage':md['url'],'spec_sha256':hashlib.sha256(text).hexdigest()},
                               {**result,'error':result['metadata_error']})
    assert found['source_repository']=='https://github.com/Team/Widget'
    assert found['go_module']==md['go_module']


def test_native_conditional_source_uses_actual_rpm_context():
    text=SPEC.replace(b'Summary: Fixture',b'''Source0: https://example.org/common-%{version}.tar.gz
%if 1
Source1: https://example.org/active.tar.gz
%else
Source1: https://example.org/inactive.tar.gz
%endif
Patch0: not-a-source.patch
Summary: Fixture''')
    result=native_spec.describe(text)
    assert result['metadata_error'] is None
    assert [s['number'] for s in result['metadata']['sources']]==[0,1]
    assert result['metadata']['sources'][1]['url'].endswith('active.tar.gz')


@pytest.mark.parametrize('declaration,tail,expected', [
    (b'BuildSystem: fixture\n', b'', 'fixture'),
    (b'%global chosen fixture\nBuildSystem: %{chosen}\n', b'', 'fixture'),
    (b'', b'', None),
    (b'%if 0\nBuildSystem: fixture\n%endif\n', b'', None),
    (b'BuildSystem: fixture\n', b'\n%build\n: manual\n', 'fixture'),
    (b'%global buildsystem fixture\n', b'', None),
    (b'', b'\nBuildSystem: fixture\n', None),
])
def test_buildsystem_is_expanded_main_preamble(declaration, tail, expected):
    macro = b'%buildsystem_fixture_conf() %nil\n%buildsystem_fixture_build() %nil\n%buildsystem_fixture_install() %nil\n'
    provenance = {'path': 'fixture/macros.buildsystem', 'sha256': hashlib.sha256(macro).hexdigest()}
    spec = b'Name: fixture\nVersion: 1\nRelease: 1\nSummary: Fixture\nLicense: MIT\nSource0: fixture.tar.gz\n' + declaration + b'%description\nFixture\n' + tail
    result = native_spec.describe(spec, macros=[(provenance, macro)])
    assert result['metadata_error'] is None
    assert result['metadata']['buildsystem'] == expected
