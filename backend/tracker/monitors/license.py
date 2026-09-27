"""Upgrade-only declared-license metadata comparison, not legal classification."""

from license_expression import ExpressionError, Licensing
from packaging.licenses import InvalidLicenseExpression, canonicalize_license_expression

from tracker.monitors.model import evidence, finding, version_query as query_subject
from tracker.monitors.schedule import Schedule
from tracker.providers.release import HOSTS, inputs, read


TITLE = 'License'
VERSION = 5
SCOPE = "upgrade"
_LICENSING = Licensing()


def _canonical_expression(expression):
    """Bound provider input before either parser or Boolean simplification."""
    if not isinstance(expression, str) or len(expression) > 2048:
        raise ValueError("license expression exceeds text budget")
    if len(expression.replace("(", " ( ").replace(")", " ) ").split()) > 128:
        raise ValueError("license expression exceeds token budget")
    depth = 0
    for char in expression:
        depth += (char == "(") - (char == ")")
        if depth > 16:
            raise ValueError("license expression exceeds nesting budget")
    return canonicalize_license_expression(expression)


def refresh(subject, inputs, previous):
    # Input fingerprints trigger version-pair changes; periodically catch metadata corrections.
    return Schedule(interval_seconds=43200)


def check(subject, settings, io):
    expressions = []
    originals = []
    releases = []
    for version in (subject["version"], subject["target_version"]):
        release = read(settings, version, io)
        expression = release.license_expression
        if not expression:
            return {
                "status": "unsupported",
                "findings": [],
                "note": "Comparable SPDX license expressions are missing; free text and RPM aggregate License are not compared.",
            }
        try:
            expressions.append(_canonical_expression(expression))
            originals.append(release.license_declaration)
            releases.append(release)
        except (InvalidLicenseExpression, ValueError):
            return {
                "status": "unsupported",
                "findings": [],
                "note": "Provider license expression is invalid SPDX metadata or exceeds the comparison budget.",
            }
    old, new = expressions
    findings = []
    try:
        equivalent = _LICENSING.is_equivalent(old, new, simple=True)
    except ExpressionError:
        return {
            "status": "unsupported",
            "findings": [],
            "note": "Provider SPDX expression is not supported by the equivalence parser.",
        }
    if not equivalent:
        facts = [
            evidence(
                "Declared license · " + version, expression, release.source, release.url
            )
            for version, expression, release in zip((subject["version"], subject["target_version"]), originals, releases)
        ]
        findings.append(
            finding(
                "declared-license",
                "License",
                f"{old} → {new}",
                facts,
                releases[-1].url,
                scope="upgrade",
                target_version=subject["target_version"],
            )
        )
    return {
        "status": "ok",
        "findings": findings,
        "note": "Compared same-provider, same-project SPDX metadata for this version pair.",
    }
