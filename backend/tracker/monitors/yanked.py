"""A registry's current-release withdrawal assertion, independent of upgrades."""

from tracker.monitors.model import evidence, finding, version_query as query_subject
from tracker.monitors.issues import Issue
from tracker.monitors.schedule import Schedule
from tracker.providers import release
from tracker.providers.model import UnsupportedRelease


TITLE = Issue.YANKED
VERSION = 3
SCOPE = "current"
HOSTS = set().union(*(backend.HOSTS for backend in release.BACKENDS.values()
                      if callable(getattr(backend, 'withdrawal', None))))


def inputs(package, configured):
    return release.inputs(package, configured, capability='withdrawal')


def refresh(subject, inputs, previous):
    # Yank status can change while the installed source version remains identical.
    return Schedule(interval_seconds=21600)


def check(subject, settings, io):
    try:
        metadata = release.read(settings, subject["version"], io, capability='withdrawal')
    except UnsupportedRelease as error:
        return {'status': 'unsupported', 'findings': [], 'note': str(error)}
    name, url, source = metadata.name, metadata.url, metadata.source
    yanked = metadata.yanked
    if type(yanked) is not bool:
        return {"status": "unsupported", "findings": [],
                "note": "Registry did not provide a release-level yanked assertion."}
    findings = []
    if yanked:
        facts = [evidence("Release " + metadata.withdrawal_field, True, source, url, code="release_yanked")]
        reason = metadata.yanked_reason
        if reason is not None and not isinstance(reason, str):
            raise ValueError("Registry yanked reason is not text")
        if isinstance(reason, str) and reason.strip():
            # Reject instead of silently truncating a source assertion.
            if len(reason) > 1024:
                raise ValueError("Registry yanked reason exceeds text budget")
            facts.append(evidence("Reason", reason, source, url, code="yanked_reason"))
        findings.append(finding("release-yanked", Issue.YANKED, f"{name} {subject['version']}", facts, url))
    return {"status": "ok", "findings": findings, "note": None}
