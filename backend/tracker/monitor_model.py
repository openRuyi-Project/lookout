"""Pure monitor facts/projections. No provider imports, network or persistence."""

import hashlib
import json
import re
from urllib.parse import urlsplit
from . import state


def subject(snapshot, name):
    source = snapshot.get('sources', {}).get(name, {})
    return {'name': name, 'version': source.get('version'),
            'revision': source.get('srcmd5') or source.get('native_query', {}).get('spec_sha256')}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def evidence(key, value, source, url, *, status="observed"):
    return dict(key=key, value=value, source=source, url=url, status=status)


def finding(identity, label, title, facts, evidence_url, *, scope="current", tags=(), target_version=None):
    result = dict(
        id=identity,
        label=label,
        title=title,
        facts=facts,
        evidence_url=evidence_url,
        scope=scope,
        tags=list(tags),
        target_version=target_version,
    )
    validate_findings([result])
    return result


def validate_url(value):
    if not isinstance(value, str) or len(value) > 8192:
        raise ValueError("invalid evidence URL")
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("monitor evidence must be a public HTTPS link")


def validate_findings(findings):
    if not isinstance(findings, list) or len(findings) > 1000:
        raise ValueError("monitor findings must be a bounded list")
    ids = set()
    for item in findings:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "label",
            "title",
            "facts",
            "evidence_url",
            "scope",
            "tags",
            "target_version",
        }:
            raise ValueError("invalid monitor finding fields")
        for key in ("id", "label", "title"):
            if not isinstance(item[key], str) or not item[key] or len(item[key]) > 8192:
                raise ValueError("invalid monitor finding text")
        if item["id"] in ids:
            raise ValueError("duplicate monitor finding id")
        ids.add(item["id"])
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,39}", item["label"]):
            raise ValueError("maintenance labels must be single-word identifiers")
        validate_url(item["evidence_url"])
        if item["scope"] not in ("current", "upgrade"):
            raise ValueError("invalid finding classification")
        if item["scope"] == "upgrade" and not state.usable_version(item["target_version"]):
            raise ValueError("upgrade finding requires its target version")
        if (
            not isinstance(item["tags"], list)
            or len(item["tags"]) > 8
            or any(not isinstance(t, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,39}", t) for t in item["tags"])
        ):
            raise ValueError("invalid finding tags")
        facts = item["facts"]
        if not isinstance(facts, list) or len(facts) > 256:
            raise ValueError("invalid evidence list")
        for fact in facts:
            if not isinstance(fact, dict) or set(fact) != {"key", "value", "source", "url", "status"}:
                raise ValueError("invalid evidence fields")
            if any(not isinstance(fact[k], str) or not fact[k] or len(fact[k]) > 256 for k in ("key", "source")):
                raise ValueError("invalid evidence identity")
            validate_url(fact["url"])
            if fact["status"] not in ("observed", "unavailable", "not_applicable", "not_evaluated"):
                raise ValueError("invalid evidence status")
            value = fact["value"]
            if fact["status"] != "observed":
                if value is not None:
                    raise ValueError("unobserved evidence cannot have a value")
            elif not (
                type(value) in (str, bool, int, float)
                or isinstance(value, list)
                and all(isinstance(v, str) for v in value)
            ):
                raise ValueError("invalid evidence value")
            if len(json.dumps(value, allow_nan=False)) > 16384:
                raise ValueError("evidence value exceeds budget")


def project(snapshot, name, now, latest=None, upgrading=False):
    observations = snapshot.get("monitors", {}).get(name, {})
    current = subject(snapshot, name)
    findings, checks = [], []
    ttl = snapshot.get("monitor_stale_after_seconds", 86400)
    for provider, observation in sorted(observations.items()):
        expected = {**current, "target_version": latest} if observation.get("scope") == "upgrade" else current
        same = observation.get("subject") == expected
        old = state.stale(observation, now, ttl)
        status = observation.get("status", "pending") if same else "input_changed"
        if same and old and status == "ok":
            status = "expired"
        compatible = True
        try:
            validate_findings(observation.get("findings", []))
        except (ValueError, TypeError):
            compatible = False
            status = "schema_changed" if same else status
        checks.append(
            {
                "monitor": provider,
                "changed_at": observation.get("changed_at"),
                "evidence_revision": observation.get("evidence_revision"),
                "status": status,
                "checked_at": observation.get("checked_at"),
                "attempted_at": observation.get("attempted_at"),
                "note": observation.get("note"),
                "error": observation.get("error"),
            }
        )
        if same and compatible:
            for f in observation.get("findings", []):
                if f["scope"] == "upgrade" and (not upgrading or f.get("target_version") != latest):
                    continue
                findings.append(
                    {
                        **f,
                        "id": provider + ":" + f["id"],
                        "monitor": provider,
                        "stale": old or status not in ("ok", "partial"),
                    }
                )
    return {"findings": findings, "checks": checks, "summary": summarize(findings)}


def summarize(findings):
    groups = {}
    for f in findings:
        label = f["label"]
        group = groups.setdefault(label, {"label": label, "count": 0, "stale": False})
        group["count"] += 1
        group["stale"] |= f["stale"]
        for tag in f["tags"]:
            extra = groups.setdefault(tag, {"label": tag, "count": 0, "stale": False})
            extra["count"] += 1
            extra["stale"] |= f["stale"]
    return list(groups.values())
