"""Read the optional, service-owner-only GitHub credential file."""
import os
import re
import stat
from pathlib import Path


def github_token(path):
    if path is None:
        return None
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ValueError('GitHub credential file is missing or inaccessible') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) not in (0o400, 0o600):
            raise ValueError('GitHub credential file must be owned by the service user with mode 0600 or 0400')
        data = os.read(fd, 4097)
    finally:
        os.close(fd)
    if len(data) > 4096:
        raise ValueError('GitHub credential file exceeds 4096 bytes')
    try:
        value = data.decode('ascii').strip()
    except UnicodeDecodeError:
        raise ValueError('invalid GitHub credential file') from None
    match = re.fullmatch(r'LOOKOUT_GITHUB_TOKEN=([A-Za-z0-9_]+)', value)
    if not match:
        raise ValueError('credential file must contain only LOOKOUT_GITHUB_TOKEN=TOKEN')
    return match[1]
