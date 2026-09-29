"""Bounded projection of go.mod module/retract directives, never code execution."""
import re

from tracker.monitors.source.release import semver
from tracker.providers.model import UnsupportedRelease


_TOKEN = re.compile(r'\s+|//.*|"(?:\\[^\r\n]|[^"\\\r\n])*"|`[^`\r\n]*`|[()\[\],]|'
                    r'(?:(?!//|/\*|\*/)[^\s"`()\[\],])+')


def tokens(line):
    result, position = [], 0
    while position < len(line):
        match = _TOKEN.match(line, position)
        if match is None:
            raise UnsupportedRelease('Unsupported go.mod string or comment syntax.')
        token = match[0]
        position = match.end()
        if token.startswith('//'):
            break
        if token.isspace():
            continue
        if token.startswith(('"', '`')):
            if token[0] == '"' and '\\' in token:
                raise UnsupportedRelease('Escaped go.mod strings are not supported by this projection.')
            token = token[1:-1]
        result.append(token)
    return result


def version_key(version):
    if not isinstance(version, str) or not version.startswith('v'):
        raise ValueError('Go version is not canonical')
    match = semver(version[1:])
    if not match:
        raise ValueError('Go version is not canonical')
    prerelease = tuple((0, int(part)) if part.isdigit() else (1, part)
                      for part in (match['preview'] or '').split('.') if part)
    return tuple(map(int, match['base'].split('.'))), not bool(prerelease), prerelease


def retractions(document, module):
    if len(document) > 512 * 1024 or len(document.splitlines()) > 20000:
        raise ValueError('go.mod exceeds parsing budget')
    block, names, ranges = None, [], []
    for raw in document.splitlines():
        words = tokens(raw)
        if not words:
            continue
        if words == [')']:
            if block is None:
                raise ValueError('unexpected go.mod block end')
            block = None
            continue
        if block is None and len(words) == 2 and words[1] == '(':
            block = words[0]
            continue
        directive, values = (block, words) if block else (words[0], words[1:])
        if directive == 'module':
            if len(values) != 1:
                raise ValueError('invalid go.mod module directive')
            names.append(values[0])
        elif directive == 'retract':
            if len(values) == 5 and (values[0], values[2], values[4]) == ('[', ',', ']'):
                low, high = values[1], values[3]
            elif len(values) == 1:
                low = high = values[0]
            else:
                raise ValueError('invalid go.mod retract directive')
            low, high = version_key(low), version_key(high)
            if low > high or len(ranges) >= 1024:
                raise ValueError('invalid or excessive go.mod retractions')
            ranges.append((low, high))
    if block is not None or names != [module]:
        raise ValueError('go.mod module identity or block structure is invalid')
    return ranges
