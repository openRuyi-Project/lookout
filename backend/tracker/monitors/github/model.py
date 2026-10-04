"""GitHub activity facts and matching policy; no network or storage access."""
import re
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from tracker.monitors.schedule import Schedule


class Repository(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_root: str = 'SPECS'
    aliases: dict[str, list[str]] = Field(default_factory=dict)
    ambiguous_names: list[str] = Field(default_factory=list)


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    repositories: dict[str, Repository] = Field(default_factory=dict)
    interval_seconds: int = Field(600, ge=60, le=86400)
    stale_after_seconds: int = Field(1800, ge=120, le=604800)
    request_budget: int = Field(60, ge=1, le=1000)
    reconcile_seconds: int = Field(604800, ge=3600, le=2592000)


def settings(config):
    result = Settings.model_validate(config.get('github', config.get('openruyi', {}).get('github', {})))
    if result.stale_after_seconds <= result.interval_seconds:
        raise ValueError('github stale threshold must exceed interval')
    for name, repository in result.repositories.items():
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+', name):
            raise ValueError('github repository must be OWNER/REPO')
        if name.split('/')[1] in ('.', '..'):
            raise ValueError('github repository must be OWNER/REPO')
        path = PurePosixPath(repository.source_root)
        if path.is_absolute() or '..' in path.parts or not path.parts:
            raise ValueError('github source_root must be a relative repository directory')
        repository.source_root = path.as_posix()
    return result


def polling(config, *, authenticated=False):
    interval = max(settings(config).interval_seconds, 0 if authenticated else 600)
    return Schedule(interval, 300, 3600)


class Matcher:
    """Exact catalog identities; prose tokens never manufacture package names."""
    def __init__(self, snapshot, policy):
        from tracker import state
        self.paths = {name: name for name in state.package_names(snapshot)}
        self.names = {}
        for name in self.paths:
            self.names.setdefault(name.casefold(), set()).add(name)
        for name, aliases in policy.aliases.items():
            if name not in self.paths:
                continue
            for alias in aliases:
                self.names.setdefault(alias.casefold(), set()).add(name)
        self.ambiguous = {name.casefold() for name in policy.ambiguous_names}
        self.root = policy.source_root.rstrip('/') + '/'
        self.path_pattern = re.compile(r'(?<![\w./-])' + re.escape(self.root) + r'([^\s/`<>"\[\]()]+)(?:/[^\s`<>"\[\]()]*)?')

    def path(self, path):
        if not isinstance(path, str) or not path.startswith(self.root) or '..' in PurePosixPath(path).parts:
            return None
        return self.paths.get(path[len(self.root):].split('/', 1)[0])

    def match(self, item):
        result = {}
        def add(name, kind, value):
            result.setdefault(name, set()).add((kind, value))
        for path in item.get('paths', []):
            if name := self.path(path):
                add(name, 'changed_path', path)
        for field in ('title', 'body'):
            value = item.get(field) or ''
            for matched in self.path_pattern.finditer(value):
                if name := self.path(matched[0]):
                    add(name, field + '_path', matched[0])
            explicit = {m[1].casefold() for m in re.finditer(r'`([^`\n]+)`', value)}
            prefix = re.match(r'^\s*(?:\[([^\]]+)\]|([\w.+-]+)\s*:)', value)
            if prefix:
                explicit.add((prefix[1] or prefix[2]).casefold())
            for match in re.finditer(r'[\w.+-]+', value):
                token = match[0]
                key = token.casefold()
                owners = self.names.get(key, ())
                if len(owners) != 1:
                    continue
                if (len(key) < 4 or key in self.ambiguous) and key not in explicit:
                    continue
                add(next(iter(owners)), field + '_name', token)
        for label in item.get('labels', []):
            if label.startswith('package:'):
                owners = self.names.get(label.removeprefix('package:').strip().casefold(), ())
                if len(owners) == 1:
                    add(next(iter(owners)), 'package_label', label)
        return {name: [{'kind': kind, 'value': value} for kind, value in sorted(evidence)]
                for name, evidence in result.items()}
