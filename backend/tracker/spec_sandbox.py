"""Linux-only, fail-closed confinement for the one-shot native RPM worker.

Landlock controls filesystem access; libseccomp supplies a syscall allow-list.
No namespace privilege, container socket or privileged runtime is required.
"""
import ctypes
import errno
import os
import platform
import resource
import sys
from pathlib import Path

POLICY = 1
MIN_ABI = 6  # includes ioctl restrictions and signal/abstract-UNIX scoping
MEMORY_BYTES = 256 * 1024 * 1024
PROCESS_LIMIT = 256  # shared real-UID count, not a private per-worker cgroup


class SandboxUnavailable(RuntimeError):
    pass


class Ruleset(ctypes.Structure):
    _fields_ = [('filesystem', ctypes.c_uint64), ('network', ctypes.c_uint64),
                ('scoped', ctypes.c_uint64)]


class PathRule(ctypes.Structure):
    # Linux UAPI uses this packed 12-byte layout; make ctypes packing explicit.
    _layout_ = "ms"
    _pack_ = 1
    _fields_ = [('access', ctypes.c_uint64), ('parent_fd', ctypes.c_int)]


class ArgCompare(ctypes.Structure):
    _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_uint),
                ('a', ctypes.c_uint64), ('b', ctypes.c_uint64)]


def _limits():
    # Global-root and privileged callers are exempt from RLIMIT_NPROC. Do not
    # pretend that merely setting that limit is sufficient in those environments.
    status = Path('/proc/self/status').read_text()
    cap = next(line.split()[1] for line in status.splitlines() if line.startswith('CapEff:'))
    mapping = [line.split() for line in Path('/proc/self/uid_map').read_text().splitlines()]
    if int(cap, 16) or (os.getuid() == 0 and any(int(a) == 0 and int(b) == 0 for a, b, _ in mapping)):
        raise SandboxUnavailable('unprivileged worker required for process limits')
    for kind, bound in [(resource.RLIMIT_AS, MEMORY_BYTES), (resource.RLIMIT_CPU, 3),
                        (resource.RLIMIT_NPROC, PROCESS_LIMIT), (resource.RLIMIT_NOFILE, 32),
                        (resource.RLIMIT_FSIZE, 2 * 1024 * 1024), (resource.RLIMIT_CORE, 0)]:
        _, hard = resource.getrlimit(kind)
        bound = min(bound, hard) if hard != resource.RLIM_INFINITY else bound
        resource.setrlimit(kind, (bound, bound))


def _landlock(inputs, work):
    if platform.machine() not in ('x86_64', 'aarch64', 'riscv64'):
        raise SandboxUnavailable('unsupported Landlock syscall architecture')
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(444, ctypes.c_void_p(), ctypes.c_size_t(0), ctypes.c_uint(1))
    if abi < MIN_ABI:
        raise SandboxUnavailable('Landlock ABI 6 or newer required')
    if libc.prctl(38, 1, 0, 0, 0):  # PR_SET_NO_NEW_PRIVS
        raise SandboxUnavailable('no-new-privileges unavailable')
    attrs = Ruleset((1 << 16) - 1, 3, 3)  # all ABI5 fs rights; no TCP; both scopes
    fd = libc.syscall(444, ctypes.byref(attrs), ctypes.sizeof(attrs), 0)
    if fd < 0:
        raise SandboxUnavailable('Landlock ruleset unavailable')
    read = (1 << 0) | (1 << 2) | (1 << 3)
    write = read | sum(1 << bit for bit in (1, 4, 5, 7, 8, 12, 13, 14))
    runtime = Path(sys.prefix).resolve()
    if runtime == Path('/'):
        os.close(fd)
        raise SandboxUnavailable('unsafe Python runtime prefix')
    paths = [('/usr', read), (str(runtime), read), (str(inputs), read), (str(work), write)]
    paths += [(p, read) for p in ('/etc/rpm',) if Path(p).exists()]
    paths += [(p, 1 << 2) for p in ('/etc/ld.so.cache', '/etc/localtime', '/etc/os-release') if Path(p).exists()]
    paths += [('/dev/null', (1 << 1) | (1 << 2)), ('/dev/urandom', 1 << 2)]
    try:
        for path, rights in paths:
            parent = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(rights, parent)
                if libc.syscall(445, fd, 1, ctypes.byref(rule), 0):
                    raise SandboxUnavailable('Landlock path rule rejected')
            finally:
                os.close(parent)
        if libc.syscall(446, fd, 0):
            raise SandboxUnavailable('Landlock enforcement rejected')
    finally:
        os.close(fd)
    return abi


def _seccomp():
    lib = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                         ctypes.c_uint, ctypes.POINTER(ArgCompare)]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    deny = 0x00050000 | errno.EPERM
    allow = 0x7fff0000
    ctx = lib.seccomp_init(deny)
    if not ctx:
        raise SandboxUnavailable('seccomp context unavailable')
    # Omitted syscalls fail with EPERM, including all sockets (also AF_UNIX),
    # ptrace/process_vm*, kill/pidfd*, namespace/mount, io_uring and keyring APIs.
    # chmod/chown/time updates are omitted: Landlock does not mediate all metadata.
    names = '''read write readv writev close close_range fstat stat lstat newfstatat statx
      access faccessat faccessat2 open openat lseek pread64 pwrite64 readlink readlinkat
      getdents getdents64 getcwd chdir fchdir dup dup2 dup3 pipe pipe2 fcntl ioctl
      poll ppoll select pselect6 epoll_create1 epoll_ctl epoll_wait epoll_pwait
      mmap mmap2 mprotect munmap mremap madvise brk futex futex_time64 rseq
      set_tid_address set_robust_list arch_prctl rt_sigaction rt_sigprocmask
      rt_sigreturn sigaltstack rt_sigsuspend restart_syscall
      getpid getppid gettid getuid geteuid getgid getegid getgroups getpgrp
      getrlimit getrusage sysinfo uname getrandom clock_gettime clock_getres
      clock_nanosleep nanosleep gettimeofday times time umask
      mkdir mkdirat rmdir unlink unlinkat rename renameat renameat2 link linkat
      symlink symlinkat truncate ftruncate fsync fdatasync
      fork vfork wait4 waitid execve execveat exit exit_group'''.split()
    def rule(name, action=allow, comparisons=()):
        number = lib.seccomp_syscall_resolve_name(name.encode())
        if number < 0:  # not present on this native architecture
            return
        array = (ArgCompare * len(comparisons))(*comparisons) if comparisons else None
        if lib.seccomp_rule_add_array(ctx, action, number, len(comparisons), array):
            raise SandboxUnavailable('seccomp rule rejected')
    try:
        for name in names:
            rule(name)
        # glibc falls back to clone/vfork on ENOSYS, not EPERM, for clone3.
        rule('clone3', 0x00050000 | errno.ENOSYS)
        for flags in (17, 0x4111, 0x1200011):
            rule('clone', comparisons=(ArgCompare(0, 4, flags, 0),))  # SCMP_CMP_EQ
        # A helper may inspect its own limits, never change another process's.
        rule('prlimit64', comparisons=(ArgCompare(0, 4, 0, 0), ArgCompare(2, 4, 0, 0)))
        if lib.seccomp_load(ctx):
            raise SandboxUnavailable('seccomp enforcement rejected')
    finally:
        lib.seccomp_release(ctx)


def confine(inputs, work):
    _limits()
    # Load the library before denying filesystem access. Its dependency paths are
    # immutable runtime, not application state or operator configuration.
    ctypes.CDLL('libseccomp.so.2')
    abi = _landlock(inputs, work)
    _seccomp()
    return {'policy': POLICY, 'landlock_abi': abi, 'seccomp': 'allow-list',
            'memory_bytes': MEMORY_BYTES, 'process_limit': PROCESS_LIMIT}
