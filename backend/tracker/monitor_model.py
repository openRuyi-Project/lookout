"""Pure monitor facts/projections. No provider imports, network or persistence."""

import hashlib
import json
from urllib.parse import urlsplit
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, ValidationInfo, field_validator, model_validator
from . import state, version_status


CORE_IDS = frozenset(('source', 'version', 'build'))


def subject(snapshot, name):
    source = state.current_source(snapshot, name)
    return {'name': name, 'version': source.get('version'),
            'revision': source.get('revision')}


def version_query(subject, inputs):
    """Dependencies of external version-based evidence, not local patch applicability."""
    return {key: subject.get(key) for key in ('version', 'target_version') if key in subject}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def evidence(key, value, source, url, *, status="observed", code=None):
    result = dict(key=key, value=value, source=source, url=url, status=status)
    if code is not None:
        result['code'] = code
    return result


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
    return value


Text = Annotated[str, Field(min_length=1, max_length=8192)]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9]{0,39}$")]
EvidenceURL = Annotated[str, AfterValidator(validate_url)]


class Evidence(BaseModel):
    """Attributed provider fact, shared by ingestion and read-only responses."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    key: Annotated[str, Field(min_length=1, max_length=256)]
    code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")] | None = None
    value: str | bool | int | float | list[str] | None
    source: Annotated[str, Field(min_length=1, max_length=256)]
    url: EvidenceURL
    status: Literal["observed", "unavailable", "not_applicable", "not_evaluated"]

    @field_validator("code")
    @classmethod
    def supplied_code(cls, value, info: ValidationInfo):
        # Stored facts omit absent codes; legacy API responses emitted code:null.
        if value is None and info.context and info.context.get("stored_fact"):
            raise ValueError("an evidence code must be a nonempty identifier when supplied")
        return value

    @model_validator(mode="after")
    def checked_value(self) -> Self:
        if self.status == "observed" and self.value is None:
            raise ValueError("observed evidence requires a value")
        if self.status != "observed" and self.value is not None:
            raise ValueError("unobserved evidence cannot have a value")
        if len(json.dumps(self.value, allow_nan=False)) > 16384:
            raise ValueError("evidence value exceeds budget")
        return self


class RawFinding(BaseModel):
    """Persisted monitor contract; API projections may add fields, not relax it."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: Text
    label: Identifier
    title: Text
    facts: Annotated[list[Evidence], Field(max_length=256)]
    evidence_url: EvidenceURL
    scope: Literal["current", "upgrade"]
    tags: Annotated[list[Identifier], Field(max_length=8)]
    target_version: str | None

    @model_validator(mode="after")
    def upgrade_target(self) -> Self:
        if self.scope == "upgrade" and not state.usable_version(self.target_version):
            raise ValueError("upgrade finding requires its target version")
        return self


_FINDINGS = TypeAdapter(Annotated[list[RawFinding], Field(max_length=1000)])


def validate_findings(findings):
    # Validate without rewriting provider dictionaries or adding optional defaults.
    parsed = _FINDINGS.validate_python(findings, strict=True, context={"stored_fact": True})
    if any(not isinstance(item, dict) or any(not isinstance(fact, dict) for fact in item["facts"])
           for item in findings):
        raise ValueError("stored findings and evidence must be dictionaries")
    if len({item.id for item in parsed}) != len(parsed):
        raise ValueError("duplicate monitor finding id")


def project(snapshot, name, now, *, version=None):
    observations = snapshot.get("monitors", {}).get(name, {})
    version = version or version_status.evaluate(snapshot, name, now)
    current = version.subject
    latest = version.upstream.get('version')
    source_unavailable = bool(version.source.get('error')) or version.source_stale
    findings, checks = [], []
    ttl = snapshot.get("monitor_stale_after_seconds", 86400)
    for provider, observation in sorted(observations.items()):
        expected = {**current, "target_version": latest} if observation.get("scope") == "upgrade" else current
        same = observation.get("subject") == expected
        old = source_unavailable or state.stale(observation, now, ttl)
        status = observation.get("status", "pending") if same else "input_changed"
        gated = same and observation.get('input_status') == 'unsupported'
        if gated:
            status = 'input_unavailable'
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
                "note": observation.get("input_note") if gated else observation.get("note"),
                "error": observation.get("error"),
            }
        )
        if same and compatible:
            for f in observation.get("findings", []):
                if f["scope"] == "upgrade" and (version.last_known_relation != 'outdated'
                                                or f.get("target_version") != latest):
                    continue
                findings.append(
                    {
                        **f,
                        "id": provider + ":" + f["id"],
                        "monitor": provider,
                        "stale": old or status not in ("ok", "partial")
                        or f['scope'] == 'upgrade' and not version.upgrading,
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
