"""A registry's current-release withdrawal assertion, independent of upgrades."""

from .monitor_model import evidence, finding, version_query as query_subject
from .release_metadata import HOSTS, inputs, read
from .schedule import Schedule


TITLE = "Yanked"
VERSION = 1
SCOPE = "current"


def refresh(subject, inputs, previous):
    # Yank status can change while the installed source version remains identical.
    return Schedule(interval_seconds=21600)


def check(subject, settings, io):
    release = read(settings, subject["version"], io)
    name, url, source = release.name, release.url, release.source
    yanked = release.yanked
    if type(yanked) is not bool:
        return {"status": "unsupported", "findings": [],
                "note": "Registry did not provide a release-level yanked assertion."}
    findings = []
    if yanked:
        facts = [evidence("Release yanked", True, source, url, code="release_yanked")]
        reason = release.yanked_reason
        if reason is not None and not isinstance(reason, str):
            raise ValueError("Registry yanked reason is not text")
        if isinstance(reason, str) and reason.strip():
            # Reject instead of silently truncating a source assertion.
            if len(reason) > 1024:
                raise ValueError("Registry yanked reason exceeds text budget")
            facts.append(evidence("Reason", reason, source, url, code="yanked_reason"))
        findings.append(finding("release-yanked", "Yanked", f"{name} {subject['version']}", facts, url))
    return {"status": "ok", "findings": findings, "note": None}
