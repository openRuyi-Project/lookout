"""OBS status semantics, shared by display and package selection.

Blocked is an issue (a dependency prevents progress), not a failed build.
Queued/running work and deliberately disabled targets are not issues.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Status:
    text: str
    kind: str
    rank: int
    issue: bool = False


STATES = {
    'succeeded': Status('✓', 'ok', 80),
    'failed': Status('Failed', 'error', 0, issue=True),
    'unresolvable': Status('Unresolvable', 'error', 1, issue=True),
    'broken': Status('Broken', 'error', 2, issue=True),
    'blocked': Status('Blocked', 'working', 20, issue=True),
    'building': Status('Building', 'working', 30),
    'scheduled': Status('Scheduled', 'working', 40),
    'signing': Status('Signing', 'working', 45),
    'finished': Status('Finishing', 'working', 46),
    'dispatching': Status('Dispatching', 'working', 47),
    'disabled': Status('Disabled', 'muted', 90),
    'excluded': Status('Excluded', 'muted', 91),
    'unknown': Status('No result', 'muted', 10),
}


def describe(code):
    return STATES.get(code, Status(code, 'muted', 10))


def label(code):
    return 'Issues' if code == 'issues' else 'Succeeded' if code == 'succeeded' else describe(code).text
