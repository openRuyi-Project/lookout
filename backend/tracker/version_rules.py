"""One editable rule source; deterministic expansion to native nvchecker entries."""

import json
import os
import re
import tomllib
from collections import defaultdict
from dataclasses import dataclass

BINDING_KEYS = {"compare", "watch", "track_label", "comparable", "not_applicable"}
TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class RuleOrigin:
    file: str
    table: tuple[str, ...]
    overrides_group: bool = False


@dataclass
class RuleSet:
    entries: dict
    origins: dict[str, RuleOrigin]
    bindings: dict
    options: dict


def substitute(value, parameters):
    if isinstance(value, str):

        def replace(match):
            parameter = parameters.get(match[1])
            if not isinstance(parameter, str):
                raise ValueError("missing string parameter: " + match[1])
            return parameter

        return TOKEN.sub(replace, value)
    if isinstance(value, list):
        return [substitute(item, parameters) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, parameters) for key, item in value.items()}
    return value


def value(v):
    if isinstance(v, dict):
        return "{" + ", ".join(json.dumps(k) + " = " + value(x) for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(value(x) for x in v) + "]"
    return json.dumps(v, ensure_ascii=False)


def expand(text):
    raw = tomllib.loads(text)
    if "schema" not in raw:
        return {k: v for k, v in raw.items() if k != "__config__"}, {}, {}, raw.get("__config__", {})
    if raw["schema"] != 1 or set(raw) - {"schema", "__config__", "group", "package", "binding"}:
        raise ValueError("invalid centralized version configuration")
    entries, origins = {}, {}

    def add(name, entry, origin):
        if not isinstance(name, str) or not name or name.startswith("__") or name in entries:
            raise ValueError("duplicate or invalid version owner: " + str(name))
        if not isinstance(entry, dict) or not isinstance(entry.get("source"), str):
            raise ValueError("version entry requires native source")
        entries[name] = entry
        origins[name] = origin

    for group, definition in raw.get("group", {}).items():
        definition = dict(definition)
        prefix = definition.pop("match_prefix", None)
        pattern = definition.pop("match_source", None)
        if prefix is not None:
            if (
                not isinstance(prefix, str)
                or not prefix
                or not isinstance(pattern, str)
                or "version" not in re.compile(pattern).groupindex
            ):
                raise ValueError(
                    "automatic rules require a nonempty match_prefix and match_source with version capture"
                )
        elif pattern is not None:
            raise ValueError("match_source requires match_prefix")
        members = definition.pop("packages", {} if prefix is not None else None)
        name_template = definition.pop("package", "{name}")
        if not isinstance(members, (list, dict)):
            raise ValueError("group packages must be an explicit list or parameter table")
        items = [(n, {}) for n in members] if isinstance(members, list) else members.items()
        for member, params in items:
            if not isinstance(member, str) or not isinstance(params, dict) or "name" in params:
                raise ValueError("invalid group member parameters")
            available = {
                "name": member,
                **({"upstream": member} if prefix is not None and isinstance(members, list) else {}),
                **params,
            }
            used = set(TOKEN.findall(json.dumps([name_template, definition]))) - {"name"}
            if prefix is not None and isinstance(members, list):
                used.discard("upstream")
            if set(params) != used:
                raise ValueError("member parameters must exactly match template variables")
            name = substitute(name_template, available)
            add(name, substitute(definition, available), ["group", group, "packages", member])
    for name, entry in raw.get("package", {}).items():
        add(name, entry, ["package", name])
    bindings = raw.get("binding", {})
    if not isinstance(bindings, dict) or any(
        not isinstance(v, dict) or set(v) - BINDING_KEYS for v in bindings.values()
    ):
        raise ValueError("only version policy belongs in binding tables")
    return entries, origins, bindings, raw.get("__config__", {})


def native_text(entries, options):
    return (
        "\n\n".join(
            "[" + json.dumps(n) + "]\n" + "\n".join(json.dumps(k) + " = " + value(v) for k, v in e.items())
            for n, e in {"__config__": options, **entries}.items()
        )
        + "\n"
    )


def render(entries, options=None, bindings=None):
    """Migration/rebase helper. Factor equal fields, never invent source policy."""
    buckets = defaultdict(list)
    for name, entry in sorted(entries.items()):
        identity = entry.get(entry.get("source", ""))
        name_prefix = (
            name[: -len(identity)]
            if isinstance(identity, str) and len(identity) > 1 and name.endswith(identity)
            else None
        )
        key = (
            entry.get("source"),
            name_prefix,
            tuple(sorted(entry)),
            tuple(
                (k, value(v))
                for k, v in sorted(entry.items())
                if not isinstance(v, str)
                or (entry.get("source") == "git" and k in ("prefix", "include_regex", "exclude_regex"))
            ),
        )
        buckets[key].append((name, entry))
    out = ["# Version tracking\n", "schema = 1\n"]
    if options:
        out += ["\n[__config__]\n"] + [json.dumps(k) + " = " + value(v) + "\n" for k, v in options.items()]
    counters = defaultdict(int)
    singles = []
    for key, rows in sorted(buckets.items(), key=lambda x: str(x[0])):
        if len(rows) < 3:
            singles.extend(rows)
            continue
        source = key[0]
        counters[source] += 1
        group = source if counters[source] == 1 else source + "_" + str(counters[source])
        first = rows[0][1]
        vary = {k for k, v in first.items() if any(e[k] != v for _, e in rows)}
        # Avoid interpreting literal brace identifiers in imported expressions.
        if any(TOKEN.search(v) for _, e in rows for v in e.values() if isinstance(v, str)):
            singles.extend(rows)
            continue
        if vary == {source} and key[1] is not None:
            out += ["\n[group." + json.dumps(group) + "]\n"]
            out += ["package = " + value(key[1] + "{name}") + "\n"]
            out += [json.dumps(k) + " = " + value("{name}" if k == source else v) + "\n" for k, v in first.items()]
            out += ["packages = [\n"]
            line = "  "
            for _, entry in rows:
                token = value(entry[source]) + ", "
                if len(line) + len(token) > 100 and line.strip():
                    out.append(line.rstrip() + "\n")
                    line = "  "
                line += token
            if line.strip():
                out.append(line.rstrip() + "\n")
            out += ["]\n"]
            continue
        templates = dict(first)
        params = {n: {} for n, e in rows}
        for k in vary:
            prefix = suffix = ""
            if k in ("url", "git"):
                common = os.path.commonprefix([e[k] for _, e in rows])
                boundary = max(common.rfind(ch) for ch in "/=?&")
                prefix = common[: boundary + 1]
                if all(e[k].endswith(".git") for _, e in rows):
                    suffix = ".git"
            templates[k] = prefix + "{" + k + "}" + suffix
            for n, e in rows:
                params[n][k] = e[k][len(prefix) : len(e[k]) - len(suffix) if suffix else None]
        out += ["\n[group." + json.dumps(group) + "]\n"]
        out += [json.dumps(k) + " = " + value(v) + "\n" for k, v in templates.items()]
        out += ["\n[group." + json.dumps(group) + ".packages]\n"]
        out += [json.dumps(n) + " = " + value({k: params[n][k] for k in sorted(vary)}) + "\n" for n, e in rows]
    for name, entry in sorted(singles):
        out += ["\n[package." + json.dumps(name) + "]\n"] + [
            json.dumps(k) + " = " + value(v) + "\n" for k, v in entry.items()
        ]
    for name, binding in sorted((bindings or {}).items()):
        out += ["\n[binding." + json.dumps(name) + "]\n"] + [
            json.dumps(k) + " = " + value(v) + "\n" for k, v in binding.items()
        ]
    text = "".join(out)
    assert expand(text)[0] == entries and expand(text)[2] == (bindings or {})
    return text


def files(path):
    """groups.toml selects a directory; legacy files remain single-file inputs."""
    from pathlib import Path

    path = Path(path)
    result = ([p for p in sorted(path.parent.glob('*.toml')) if p.name not in ('nvchecker.toml',)] if path.name == 'groups.toml' else [path])
    if path not in result or any(p.is_symlink() or not p.is_file() for p in result):
        raise ValueError("native configuration changed: version configuration requires regular files")
    return result


def digest(path):
    import hashlib

    parts = files(path)
    if len(parts) == 1 and parts[0].name != "groups.toml":
        return hashlib.sha256(parts[0].read_bytes()).hexdigest()
    return hashlib.sha256(
        json.dumps(
            [(p.name, hashlib.sha256(p.read_bytes()).hexdigest()) for p in parts], separators=(",", ":")
        ).encode()
    ).hexdigest()


def load(path):
    from pathlib import Path

    path = Path(path)
    entries, origins, bindings, options = expand(path.read_text())
    locations = {name: str(path) for name in entries}
    overridden = set()
    for file in files(path):
        if file == path:
            continue
        name = file.stem
        entry = tomllib.loads(file.read_text())
        if not name or name.startswith("__") or not isinstance(entry.get("source"), str):
            raise ValueError("single-package exception requires a native source: " + str(file))
        if name in entries:
            overridden.add(name)
        entries[name] = entry  # Complete replacement; never inherit hidden fields.
        origins[name] = []
        locations[name] = str(file)
    return RuleSet(
        entries,
        {name: RuleOrigin(locations[name], tuple(origins.get(name, [name])), name in overridden) for name in entries},
        bindings,
        options,
    )


def layout(entries, options=None, bindings=None, exceptions=None, automatic_filters=None, notes=None):
    """Render a directory, keeping explicit exceptions separate from groups."""
    auto = automatic_filters or {}
    listed = expand(
        "schema=1\n"
        + "".join(
            "\n[group." + json.dumps(n) + "]\n" + "".join(json.dumps(k) + " = " + value(v) + "\n" for k, v in e.items())
            for n, e in auto.items()
        )
    )[0]
    text = render({n: e for n, e in entries.items() if n not in listed}, options, bindings)
    parsed = tomllib.loads(text)
    singles = parsed.get("package", {})
    # Independent rules are always named files, not a second exceptional table.
    if singles:
        text = text[: text.index("\n[package.")] + (
            "".join(
                "\n[binding."
                + json.dumps(n)
                + "]\n"
                + "".join(json.dumps(k) + " = " + value(v) + "\n" for k, v in e.items())
                for n, e in sorted((bindings or {}).items())
            )
        )
    individual = {**singles, **(exceptions or {})}
    for n, e in auto.items():
        text += "\n[group." + json.dumps(n) + "]\n"
        for k, v in e.items():
            if k != "packages":
                text += json.dumps(k) + " = " + value(v) + "\n"
        members = e.get("packages", {})
        if isinstance(members, list):
            text += "packages = [\n"
            line = "  "
            for member in members:
                token = value(member) + ", "
                if len(line) + len(token) > 100 and line.strip():
                    text += line.rstrip() + "\n"
                    line = "  "
                line += token
            if line.strip():
                text += line.rstrip() + "\n"
            text += "]\n"
        elif members:
            text += "\n[group." + json.dumps(n) + ".packages]\n"
            text += "".join(json.dumps(k) + " = " + value(v) + "\n" for k, v in members.items())
    result = {"groups.toml": text}
    for name, entry in individual.items():
        if name in (".", "..", "groups") or "/" in name or "\\" in name:
            raise ValueError("invalid package filename: " + name)
        result[name + ".toml"] = (notes or {}).get(name, "") + "".join(
            json.dumps(k) + " = " + value(v) + "\n" for k, v in entry.items()
        )
    return result


def filters(text):
    return {name: entry for name, entry in tomllib.loads(text).get("group", {}).items() if "match_prefix" in entry}


def automatic(text, snapshot, explicit):
    """Only saved native Source0 facts select identities; never execute a SPEC."""
    entries, origins = {}, {}
    for group, definition in filters(text).items():
        prefix = definition["match_prefix"]
        pattern = re.compile(definition["match_source"])
        template = {
            k: v for k, v in definition.items() if k not in ("match_prefix", "match_source", "packages", "package")
        }
        for name, source in snapshot.get("sources", {}).items():
            if name in explicit or not name.startswith(prefix):
                continue
            fact = snapshot.get("specs", {}).get(name, {})
            meta = fact.get("metadata") or {}
            query = fact.get("native_query") or {}
            if (
                fact.get("error")
                or query.get("error")
                or source.get("error")
                or meta.get("name") != name
                or not meta.get("version")
                or source.get("version") != meta["version"]
                or not re.fullmatch(r"[a-f0-9]{64}", query.get("spec_sha256") or "")
                or query.get("context", {}).get("resolver", 0) < 6
            ):
                continue
            urls = [v.get("url", "") for v in meta.get("sources", []) if v.get("number") == 0]
            if len(urls) != 1 or "%" in urls[0]:
                continue
            match = pattern.fullmatch(urls[0].split("#", 1)[0])
            if not match or match.groupdict().get("version") != meta["version"]:
                continue
            if name in entries:
                raise ValueError("ambiguous automatic version rule: " + name)
            params = {"name": name, "suffix": name[len(prefix) :], **match.groupdict()}
            entries[name] = substitute(template, params)
            origins[name] = ["group", group]
    return entries, origins
