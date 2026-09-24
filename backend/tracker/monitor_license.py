"""Upgrade-only declared-license metadata comparison, not legal classification."""

from urllib.parse import quote
from packaging.licenses import canonicalize_license_expression, InvalidLicenseExpression
from .monitor_model import finding, evidence

TITLE = 'License'
VERSION = 2
SCOPE = "upgrade"
HOSTS = {"pypi.org"}


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
            expressions.append(canonicalize_license_expression(expression))
        except InvalidLicenseExpression:
            return {
                "status": "unsupported",
                "findings": [],
                "note": "Provider license expression is not valid SPDX metadata.",
            }
    old, new = expressions
    findings = []
    if old != new:
        facts = [
            evidence(
                "SPDX · " + version, expression, "PyPI", f"https://pypi.org/pypi/{name}/{quote(version, safe='')}/json"
            )
            for version, expression in zip((subject["version"], subject["target_version"]), expressions)
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
