"""Upgrade-only declared-license metadata comparison, not legal classification."""

from urllib.parse import quote
from packaging.licenses import canonicalize_license_expression, InvalidLicenseExpression
from license_expression import ExpressionError, Licensing
from .monitor_model import finding, evidence
from .schedule import Schedule
from .monitor_model import version_query as query_subject


TITLE = 'License'
VERSION = 3
SCOPE = "upgrade"
HOSTS = {"pypi.org"}
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


def inputs(package, configured):
    if configured is not None:
        return configured
    from .package_identity import from_native
    identity = from_native(package['identity'])
    return {'pypi': identity['name']} if identity and identity['ecosystem'] == 'PyPI' else None


def check(subject, settings, io):
    if set(settings) != {"pypi"} or not isinstance(settings["pypi"], str) or not settings["pypi"]:
        raise ValueError("license monitor requires a PyPI identity")
    name = quote(settings["pypi"], safe="")
    expressions = []
    originals = []
    for version in (subject["version"], subject["target_version"]):
        info = io.json("GET", f"https://pypi.org/pypi/{name}/{quote(version, safe='')}/json")["info"]
        expression = info.get("license_expression")
        if not expression:
            return {
                "status": "unsupported",
                "findings": [],
                "note": "Comparable SPDX license expressions are missing; free text and RPM aggregate License are not compared.",
            }
        try:
            expressions.append(_canonical_expression(expression))
            originals.append(expression)
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
                "SPDX · " + version, expression, "PyPI", f"https://pypi.org/pypi/{name}/{quote(version, safe='')}/json"
            )
            for version, expression in zip((subject["version"], subject["target_version"]), originals)
        ]
        findings.append(
            finding(
                "declared-license",
                "LicenseChange",
                f"{old} → {new}",
                facts,
                f"https://pypi.org/project/{name}/{quote(subject['target_version'], safe='')}/",
                scope="upgrade",
                target_version=subject["target_version"],
            )
        )
    return {
        "status": "ok",
        "findings": findings,
        "note": "Compared same-provider, same-project SPDX metadata for this version pair.",
    }
