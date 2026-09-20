# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""One contributor entry: explain, check, plan, apply. No production state writes."""

import argparse
import json
from pathlib import Path
from . import config as cfg, config_change, nv, state, version_rules


def location(path, keys):
    positions, _ = config_change.table_positions(Path(path).read_text())
    span = positions.get(tuple(keys))
    return {"file": str(Path(path).resolve()), "table": list(keys), "line": span[0] + 1 if span else None}


def rule_location(config, name):
    keys = config.get("rule_origins", {}).get(name, ["package", name] if config.get("compact_versions") else [name])
    if not keys:
        return {
            "file": str(Path(config["rule_files"][name]).resolve()),
            "table": [],
            "line": 1,
            "overrides_group": name in config.get("overridden_rules", []),
            "group_origin": config.get("group_origins", {}).get(name),
        }
    if name not in config["native"] and Path(config["nvpath"]).name == "groups.toml":
        if Path(name).name != name or name in (".", "..", "groups"):
            raise ValueError("invalid package filename")
        return {"file": str(Path(config["nvpath"]).parent / (name + ".toml")), "table": [], "line": None}
    if len(keys) == 2 and keys[0] == "group":
        return {**location(config["nvpath"], keys), "match": "source_heuristic"}
    if keys[0] == "group":
        result = location(config["nvpath"], keys[:3])
        member = json.dumps(keys[3]) + " ="
        lines = Path(config["nvpath"]).read_text().splitlines()
        start = (result["line"] or 1) - 1
        result["line"] = next(
            (n + 1 for n in range(start + 1, len(lines)) if lines[n].startswith(member)), result["line"]
        )
        if result["line"] is None:
            result["line"] = location(config["nvpath"], keys[:2])["line"]
        result["table"] = keys
        return result
    return location(config["nvpath"], keys)


def explain(config_path, name, db=None, runtime_config=None):
    snapshot = state.read(db) if db else state.empty()
    config = cfg.load(config_path, snapshot=snapshot)
    binding = cfg.binding(config, name)
    ids = list(dict.fromkeys(t for t in [binding["compare"], *binding["watch"]] if t))
    rules = []
    runtime = cfg.load(runtime_config, snapshot=snapshot) if runtime_config else None
    for track in ids:
        entry = config["native"][track]
        rule = {
            "id": track,
            **rule_location(config, track),
            "fingerprint": cfg.track_fingerprint(entry),
            "source": cfg.public_source(entry),
            "policy": {
                k: v
                for k, v in entry.items()
                if k
                in (
                    "prefix",
                    "from_pattern",
                    "to_pattern",
                    "include_regex",
                    "exclude_regex",
                    "filter",
                    "use_latest_release",
                    "use_max_tag",
                    "use_pre_release",
                )
            },
            "observation": snapshot.get("tracks", {}).get(track),
        }
        if runtime is not None:
            rule["runtime_matches"] = runtime["native"].get(track) == entry
            rule["runtime_location"] = rule_location(runtime, track)
        rules.append(rule)
    spec = snapshot.get("specs", {}).get(name, {})
    origin = spec.get("source_origin") or config["spec"]
    return {
        "name": name,
        "binding": binding,
        "rules": rules,
        "binding_location": (
            location(config["nvpath"], ("binding", name))
            if name in config.get("version_bindings", {})
            else location(config_path, ("packages", name))
        ),
        "version_rule_location": rule_location(config, binding["compare"] or name),
        "binding_is_implicit": name not in config["packages"],
        "runtime_binding_matches": cfg.binding(runtime, name) == binding if runtime is not None else None,
        "spec": {
            "path": f"SPECS/{name}",
            "url": cfg.spec_source_url(origin, name, spec.get("head") or origin.get("branch") or ""),
            "spec_sha256": spec.get("native_query", {}).get("spec_sha256"),
            "error": spec.get("error"),
        },
        "config_sha256": config_change.digest(config_path),
        "native_sha256": config["nv_digest"],
        "read_only": True,
    }


def check(config_path, name, db=None):
    config = cfg.load(config_path, snapshot=state.read(db) if db else None)
    binding = cfg.binding(config, name)
    selected = list(dict.fromkeys(t for t in [binding["compare"], *binding["watch"]] if t))
    if not selected:
        raise ValueError("package has no configured track; review discovery evidence before adding a native rule")
    before = config_change.digest(db) if db and Path(db).is_file() else None
    # Never pass a production state path to a collector; no oldver/newver writes.
    facts, error = nv.run(config, {}, state.utcnow(), tracks=selected)
    if config_change.digest(config_path) != config["config_digest"]:
        raise ValueError("tracker configuration changed during check")
    if version_rules.digest(config["nvpath"]) != config["nv_digest"]:
        raise ValueError("native configuration changed during check")
    if before is not None and before != config_change.digest(db):
        # A concurrent production writer is not our write; do not assert unchanged.
        unchanged = False
    else:
        unchanged = True
    return {
        "name": name,
        "tracks": facts,
        "command_error": error,
        "passed": not error and all(f.get("version") and not f.get("error") for f in facts.values()),
        "state_writes": False,
        "input_snapshot_unchanged": unchanged,
        "native_sha256": config["nv_digest"],
    }


def human_result(action, result, report=None):
    lines = []
    if result.get("name"):
        lines.append("Package: " + result["name"])
    if action == "explain":
        rules = result["rules"]
        for rule in rules or [result["version_rule_location"]]:
            lines.append(f"Edit: {rule['file']}:{rule.get('line') or '?'}")
            if rule.get("match") == "source_heuristic":
                kind = "automatic"
            elif not rule.get("table"):
                kind = "package override"
            elif rule["table"][0] == "group":
                kind = "explicit group"
            else:
                kind = "explicit rule"
            lines.append("Rule: " + kind if rules else "Rule: untracked")
    elif action == "check":
        lines.append("Check: " + ("passed" if result["passed"] else "failed"))
        for name, fact in result["tracks"].items():
            lines.append(f"{name}: {fact.get('error') or fact.get('version') or 'no evidence'}")
        if result.get("command_error"):
            lines.append("Error: " + result["command_error"])
    elif action == "plan":
        lines.append("Changed tracks: " + (", ".join(result["changed_tracks"]) or "none"))
        protected = result.get("overridden_unaffected_tracks", [])
        if protected:
            lines.append("Unaffected overrides: " + ", ".join(protected))
    elif action == "apply":
        lines.append("Prepared: " + result["destination"])
        lines.append("Activation: select the prepared directory and restart")
    else:
        lines.append("Proposal: review the saved evidence before editing configuration")
    if report:
        lines.append("Report: " + str(Path(report).resolve()))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for action in ("explain", "check", "setup"):
        p = commands.add_parser(action)
        p.add_argument("name")
        p.add_argument("--config", required=True)
        p.add_argument("--db")
        p.add_argument("--runtime-config" if action == "explain" else "--output")
    p = commands.add_parser("plan")
    p.add_argument("--base-config", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--runtime-config", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--db")
    p = commands.add_parser("apply")
    p.add_argument("--review", required=True)
    p.add_argument("--runtime-config", required=True)
    p.add_argument("--destination", required=True)
    for command in commands.choices.values():
        command.add_argument("--format", choices=("json", "human"), default="json")
    args = parser.parse_args(argv)
    try:
        if args.command == "setup":
            from .onboarding import propose

            if not args.db or not args.output:
                raise ValueError("setup requires --db SNAPSHOT_COPY and --output NEW_JSON")
            result = propose(args.config, args.name, state.read(args.db))
            with Path(args.output).open("x") as out:
                out.write(json.dumps(result, indent=2) + "\n")
        elif args.command == "explain":
            result = explain(args.config, args.name, args.db, args.runtime_config)
        elif args.command == "check":
            result = check(args.config, args.name, args.db)
            if args.output:
                with Path(args.output).open("x") as out:
                    out.write(json.dumps(result, indent=2) + "\n")
        elif args.command == "plan":
            result = config_change.plan(
                args.base_config,
                args.config,
                args.runtime_config,
                args.output,
                snapshot=state.read(args.db) if args.db else None,
            )
        else:
            result = config_change.apply(args.review, args.runtime_config, args.destination)
    except (OSError, ValueError) as error:
        print("Error: " + str(error) if args.format == "human" else json.dumps({"error": str(error)}))
        return 2
    report = getattr(args, "output", None)
    if args.command == "plan":
        report = str(Path(args.output) / "review.json")
    print(
        human_result(args.command, result, report)
        if args.format == "human"
        else json.dumps(result, ensure_ascii=False, indent=2)
    )
    return 2 if result.get("passed") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
