"""Lifecycle adapter. Product/cycle policy belongs to package configuration."""

from datetime import date
import re
from urllib.parse import quote
from .monitor_model import finding, evidence

TITLE = 'EOL'
VERSION = 2
HOSTS = {"endoflife.date"}


def inputs(package, configured):
    return configured


def check(subject, settings, io):
    if set(settings) - {"product", "cycle_parts"}:
        raise ValueError("unsupported lifecycle configuration")
    product, parts = settings["product"], settings["cycle_parts"]
    if (
        not isinstance(product, str)
        or not re.fullmatch(r"[a-z0-9-]+", product)
        or type(parts) is not int
        or not 1 <= parts <= 3
    ):
        raise ValueError("lifecycle requires a product slug and cycle_parts in 1..3")
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:[a-z][0-9]*)?", subject["version"])
    if not match or len(match[1].split(".")) < parts:
        return {"status": "unsupported", "findings": [], "note": "Version does not establish a release cycle."}
    cycle = ".".join(match[1].split(".")[:parts])
    data = io.json("GET", "https://endoflife.date/api/v1/products/" + quote(product, safe="") + "/")
    releases = data["result"]["releases"]
    release = next((r for r in releases if r["name"] == cycle), None)
    if release is None:
        return {"status": "unsupported", "findings": [], "note": f"Provider has no cycle {cycle} for {product}."}
    eol = release.get("eolFrom")
    if eol is not None:
        ended = date.fromisoformat(eol) <= io.today
    elif type(release.get("isEol")) is bool:
        ended = release["isEol"]
    else:
        return {"status": "unsupported", "findings": [], "note": "Provider has not established an EOL date or status."}
    findings = []
    if ended:
        url = "https://endoflife.date/" + product
        facts = [
            evidence("Product", product, "endoflife.date", url),
            evidence("Cycle", cycle, "endoflife.date", url),
            evidence("EOL", ended, "endoflife.date", url),
        ]
        if eol:
            facts.append(evidence("EOL date", eol, "endoflife.date", url))
        findings.append(finding("cycle:" + cycle, "EOL", product + " " + cycle, facts, url))
    return {"status": "ok", "findings": findings, "note": f"Current source cycle {cycle}; upstream lifecycle only."}
