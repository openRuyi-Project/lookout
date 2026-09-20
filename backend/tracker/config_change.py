# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""Reviewed native-rule/binding edits, rebased onto operator configuration.

Plan never writes runtime. Apply creates a NEW configuration directory, never
replaces live mounts. The existing deployment/restart selects that directory.
"""

from pathlib import Path
import hashlib
import difflib
import json
import os
import shutil
import tempfile
import tomllib
from . import config as cfg, nv, version_rules


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def table_positions(text):
    """Locate native TOML tables using TOML itself to parse quoted/dotted keys."""
    lines = text.splitlines(keepends=True)
    headers = []
    for n, line in enumerate(lines):
        if not line.lstrip().startswith("["):
            continue
        try:
            obj = tomllib.loads(line + "\n__tracker_locator__ = true\n")
            path = []
            while isinstance(obj, dict) and len(obj) == 1:
                key, value = next(iter(obj.items()))
                if key == "__tracker_locator__":
                    break
                path.append(key)
                obj = value
            if obj == {"__tracker_locator__": True}:
                headers.append((tuple(path), n))
        except tomllib.TOMLDecodeError:
            pass  # multiline strings are rejected by the semantic postcondition
    return {
        key: (start, headers[n + 1][1] if n + 1 < len(headers) else len(lines))
        for n, (key, start) in enumerate(headers)
    }, lines


def edit_tables(text, changes, prefix=()):
    if not changes:
        return text
    expected = tomllib.loads(text)
    container = expected
    for key in prefix:
        container = container.setdefault(key, {})
    positions, lines = table_positions(text)
    edits = []
    for name, value in changes.items():
        path = (*prefix, name)
        if name in container and path not in positions:
            raise ValueError(f"format {path!r} as a separate TOML table before editing")
        if value is None:
            container.pop(name, None)
            replacement = ""
        else:
            container[name] = value
            replacement = (
                "\n["
                + ".".join(json.dumps(p) for p in path)
                + "]\n"
                + "".join(json.dumps(k) + " = " + nv._toml_value(v) + "\n" for k, v in value.items())
            )
        start, end = positions.get(path, (len(lines), len(lines)))
        edits.append((start, end, replacement))
    for start, end, value in sorted(edits, reverse=True):
        lines[start:end] = [value]
    result = "".join(lines)
    parsed = tomllib.loads(result)
    # Removing the final child also removes its implicit TOML parent table.
    for depth in range(len(prefix), 0, -1):
        parent = expected
        for part in prefix[: depth - 1]:
            parent = parent[part]
        key = prefix[depth - 1]
        if parent.get(key) == {} and tuple(prefix[:depth]) not in positions:
            parent.pop(key)
    if parsed != expected:
        raise ValueError("unsupported TOML layout; refusing lossy configuration edit")
    return result


def rebase(base, candidate, runtime):
    changes, conflicts = {}, []
    for name in sorted(base.keys() | candidate.keys()):
        old, new = base.get(name), candidate.get(name)
        if old == new:
            continue
        actual = runtime.get(name)
        if actual not in (old, new):
            conflicts.append(name)
        elif actual != new:
            changes[name] = new
    if conflicts:
        raise ValueError("operator changes conflict: " + ", ".join(conflicts))
    return changes


def inputs(path, snapshot=None):
    path = Path(path).resolve()
    config = cfg.load(path, snapshot=snapshot)
    native = Path(config["nvpath"])
    if not native.resolve().is_relative_to(path.parent) or native == path:
        raise ValueError("promotion requires version files inside the configuration directory")
    return path, native, config


def merge_text(base, candidate, runtime, name):
    """Rebase literal edits; unrelated lines and comments remain byte-for-byte."""
    if candidate == base or candidate == runtime:
        return runtime
    if runtime == base:
        return candidate
    if None in (base, candidate, runtime):
        raise ValueError("operator changes conflict: " + name)
    lines = base.splitlines(keepends=True)

    def edits(text):
        other = text.splitlines(keepends=True)
        matcher = difflib.SequenceMatcher(a=lines, b=other, autojunk=False)
        return [(a, b, other[c:d]) for op, a, b, c, d in matcher.get_opcodes() if op != "equal"]

    changes = edits(runtime)
    for start, end, replacement in edits(candidate):
        edit = (start, end, replacement)
        if edit in changes:
            continue
        for left, right, _ in changes:
            overlap = max(start, left) < min(end, right)
            insertion_collision = (start == end and left <= start <= right) or (left == right and start <= left <= end)
            if overlap or insertion_collision:
                raise ValueError("operator text changes conflict: " + name)
        changes.append(edit)
    for start, end, replacement in sorted(changes, key=lambda e: e[:2], reverse=True):
        lines[start:end] = replacement
    return "".join(lines)


def directory_texts(base, candidate, runtime):
    def contents(config):
        path = Path(config["nvpath"])
        return {p.name: p.read_text() for p in version_rules.files(path)}

    before, proposed, actual = map(contents, (base, candidate, runtime))
    result = {}
    for name in sorted(before.keys() | proposed.keys() | actual.keys()):
        text = merge_text(before.get(name), proposed.get(name), actual.get(name), name)
        if text is not None:
            result[name] = text
    return result


def plan(base_path, candidate_path, runtime_path, output, snapshot=None):
    base_file, _, base = inputs(base_path, snapshot)
    candidate_file, _, candidate = inputs(candidate_path, snapshot)
    runtime_file, native_file, runtime = inputs(runtime_path, snapshot)
    if snapshot is None and any(c.get("automatic_filters") for c in (base, candidate, runtime)):
        raise ValueError("automatic version rules require a saved snapshot; pass --db when planning")
    # This command promotes package rules, not unrelated operator/site settings.
    a, b = tomllib.loads(base_file.read_text()), tomllib.loads(candidate_file.read_text())
    for key in ("packages", "openruyi", "monitors"):
        a.pop(key, None)
        b.pop(key, None)
    a.get("collector", {}).pop("nvchecker_config", None)
    b.get("collector", {}).pop("nvchecker_config", None)
    if a != b:
        raise ValueError("change site settings separately; plan only promotes native rules and package bindings")
    base_options = tomllib.loads(Path(base["nvpath"]).read_text()).get("__config__", {})
    candidate_options = tomllib.loads(Path(candidate["nvpath"]).read_text()).get("__config__", {})
    if base_options != candidate_options:
        raise ValueError("change native operator options separately from package rules")
    native_changes = rebase(base["native"], candidate["native"], runtime["native"])
    binding_changes = rebase(base["packages"], candidate["packages"], runtime["packages"])
    appearance_changes = rebase(
        base.get("openruyi", {}).get("buildsystems", {}),
        candidate.get("openruyi", {}).get("buildsystems", {}),
        runtime.get("openruyi", {}).get("buildsystems", {}),
    )
    monitor_changes = rebase(
        {"monitors": base.get("monitors")},
        {"monitors": candidate.get("monitors")},
        {"monitors": runtime.get("monitors")},
    )
    compact = bool(candidate.get("compact_versions") or runtime.get("compact_versions"))
    tracker_changes = binding_changes
    if compact:
        operator_packages = tomllib.loads(runtime_file.read_text()).get("packages", {})
        tracker_changes = {}
        for name, entry in binding_changes.items():
            value = {k: v for k, v in (entry or {}).items() if k not in version_rules.BINDING_KEYS}
            if value != operator_packages.get(name, {}):
                tracker_changes[name] = value or None
    tracker_text = edit_tables(runtime_file.read_text(), tracker_changes, ("packages",))
    tracker_text = edit_tables(tracker_text, appearance_changes, ("openruyi", "buildsystems"))
    tracker_text = edit_tables(tracker_text, monitor_changes)
    compact = bool(candidate.get("compact_versions") or runtime.get("compact_versions"))
    directory = Path(candidate["nvpath"]).name == "groups.toml"
    output_native = (
        str(Path(candidate["nvpath"]).relative_to(candidate_file.parent))
        if compact
        else str(native_file.relative_to(runtime_file.parent))
    )
    stable_directory = directory and all(Path(c["nvpath"]).name == "groups.toml" for c in (base, runtime))
    if compact:
        merged_native = dict(runtime["native"])
        merged_bindings = {n: dict(v) for n, v in runtime["packages"].items()}
        for name, entry in native_changes.items():
            if entry is None:
                merged_native.pop(name, None)
            else:
                merged_native[name] = entry
        for name, entry in binding_changes.items():
            if entry is None:
                merged_bindings.pop(name, None)
            else:
                merged_bindings[name] = entry
        policies = {
            n: {k: v for k, v in e.items() if k in version_rules.BINDING_KEYS} for n, e in merged_bindings.items()
        }
        policies = {n: e for n, e in policies.items() if e}
        runtime_raw = tomllib.loads(tracker_text).get("packages", {})
        stripped = {
            n: ({k: v for k, v in e.items() if k not in version_rules.BINDING_KEYS} or None)
            for n, e in runtime_raw.items()
            if set(e) & version_rules.BINDING_KEYS
        }
        tracker_text = edit_tables(tracker_text, stripped, ("packages",))
        collector = tomllib.loads(tracker_text)["collector"]
        if collector["nvchecker_config"] != output_native:
            collector["nvchecker_config"] = output_native
            tracker_text = edit_tables(tracker_text, {"collector": collector})
        native_text = (
            native_file.read_text()
            if stable_directory
            else version_rules.render(merged_native, runtime.get("native_options", {}), policies)
        )
    else:
        native_text = edit_tables(native_file.read_text(), native_changes)
    texts = {output_native: native_text, runtime_file.name: tracker_text}
    if stable_directory:
        bundle = directory_texts(base, candidate, runtime)
        directory_path = Path(output_native).parent
        texts = {str(directory_path / name): body for name, body in bundle.items()}
        texts[runtime_file.name] = tracker_text
    elif directory:
        group_changes = rebase(base["group_native"], candidate["group_native"], runtime["group_native"])
        exception_changes = rebase(base["exception_native"], candidate["exception_native"], runtime["exception_native"])
        groups = dict(runtime["group_native"])
        exceptions = dict(runtime["exception_native"])
        for owner, edits in ((groups, group_changes), (exceptions, exception_changes)):
            for name, entry in edits.items():
                if entry is None:
                    owner.pop(name, None)
                else:
                    owner[name] = entry
        promoted_options = dict(runtime.get("native_options", {}))
        keyfile = promoted_options.get("keyfile")
        if keyfile and not Path(keyfile).is_absolute():
            promoted_options["keyfile"] = os.path.relpath(
                native_file.parent / keyfile, runtime_file.parent / Path(output_native).parent
            )
        filters = dict(runtime["automatic_filters"])
        notes = dict(runtime["exception_notes"])
        for values, key in ((filters, "automatic_filters"), (notes, "exception_notes")):
            for name, entry in rebase(base[key], candidate[key], runtime[key]).items():
                if entry is None:
                    values.pop(name, None)
                else:
                    values[name] = entry
        bundle = version_rules.layout(groups, promoted_options, policies, exceptions, filters, notes)
        directory_path = Path(output_native).parent
        texts = {str(directory_path / name): body for name, body in bundle.items()}
        texts[runtime_file.name] = tracker_text
    baseline_files = [runtime_file, *version_rules.files(native_file)]
    baseline_hashes = {str(p.relative_to(runtime_file.parent)): digest(p) for p in baseline_files}

    output = Path(output).resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    for name, text in texts.items():
        (output / name).parent.mkdir(parents=True, exist_ok=True)
        (output / name).write_text(text)
        (output / name).chmod(0o600)
    prepared_config = cfg.load(output / runtime_file.name, snapshot=snapshot)
    expected_native = dict(runtime["native"])
    for name, entry in native_changes.items():
        if entry is None:
            expected_native.pop(name, None)
        else:
            expected_native[name] = entry
    if compact and prepared_config["packages"] != merged_bindings:
        raise ValueError("text promotion differs from reviewed package bindings")
    if prepared_config["native"] != expected_native:
        raise ValueError("text promotion differs from reviewed effective rules")
    changed_group_members = sorted(
        n
        for n in base["group_native"].keys() | candidate["group_native"].keys()
        if base["group_native"].get(n) != candidate["group_native"].get(n)
    )
    protected = sorted(
        n for n in changed_group_members if n in prepared_config["exception_native"] and n not in native_changes
    )
    record = {
        "schema": 3 if directory else (2 if output_native != native_file.name else 1),
        "obsolete_files": sorted(set(baseline_hashes) - set(texts)),
        "obsolete_native": native_file.name if output_native != native_file.name else None,
        "runtime_config": str(runtime_file),
        "baseline_hashes": baseline_hashes,
        "proposed_hashes": {n: digest(output / n) for n in texts},
        "changed_filters": sorted(
            rebase(base["automatic_filters"], candidate["automatic_filters"], runtime["automatic_filters"])
        ),
        "inventory_generation": (snapshot or {}).get("generation"),
        "changed_tracks": sorted(native_changes),
        "overridden_unaffected_tracks": protected,
        "changed_bindings": sorted(binding_changes),
        "changed_buildsystems": sorted(appearance_changes),
        "monitor_settings_changed": bool(monitor_changes),
        "operator_options_preserved": True,
        "activation": "apply creates a fresh directory; existing deployment must select it and restart",
    }
    (output / "review.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def apply(review_dir, runtime_path, destination):
    review_dir = Path(review_dir).resolve()
    record = json.loads((review_dir / "review.json").read_text())
    runtime_file, native_file, _ = inputs(runtime_path)
    if record.get("schema") not in (1, 2, 3) or str(runtime_file) != record["runtime_config"]:
        raise ValueError("review belongs to a different runtime configuration")
    names = {str(p.relative_to(runtime_file.parent)) for p in [runtime_file, *version_rules.files(native_file)]}
    proposed = set(record["proposed_hashes"])

    def safe(n):
        p = Path(n)
        return not p.is_absolute() and ".." not in p.parts and str(p) == n and p.suffix == ".toml"

    if (
        set(record["baseline_hashes"]) != names
        or runtime_file.name not in proposed
        or any(not safe(n) for n in proposed)
        or (record["schema"] == 1 and proposed != names)
        or (record["schema"] == 2 and record.get("obsolete_native") != native_file.name)
        or (record["schema"] == 3 and set(record["obsolete_files"]) != names - proposed)
    ):
        raise ValueError("unexpected review file set")

    def unchanged():
        return {
            str(p.relative_to(runtime_file.parent)) for p in [runtime_file, *version_rules.files(native_file)]
        } == names and all(digest(runtime_file.parent / n) == record["baseline_hashes"][n] for n in names)

    if not unchanged():
        raise ValueError("runtime configuration drift; re-plan and review")
    if any(digest(review_dir / n) != record["proposed_hashes"][n] for n in proposed):
        raise ValueError("reviewed candidate changed; re-plan and review")
    destination = Path(destination).resolve()
    if destination.exists() or destination.is_relative_to(runtime_file.parent):
        raise ValueError("destination must be a fresh directory outside the runtime config")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Preserve keys/other operator files privately. Never include them in reports.
    # Reject symlinks rather than accidentally following a secret outside scope.
    if any(p.is_symlink() for p in runtime_file.parent.rglob("*")):
        raise ValueError("operator config symlinks require explicit resolution before promotion")
    with tempfile.TemporaryDirectory(prefix=".tracker-config-", dir=destination.parent) as temp:
        prepared = Path(temp) / "config"
        shutil.copytree(runtime_file.parent, prepared)
        prepared.chmod(0o700)
        for name in proposed:
            (prepared / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(review_dir / name, prepared / name)
            (prepared / name).chmod(0o600)
        for name in record.get("obsolete_files", [record["obsolete_native"]] if record.get("obsolete_native") else []):
            (prepared / name).unlink()
        cfg.load(prepared / runtime_file.name)
        if not unchanged():
            raise ValueError("runtime configuration changed during preparation")
        os.rename(prepared, destination)
    return {
        "destination": str(destination),
        "runtime_unchanged": True,
        "changed_tracks": record["changed_tracks"],
        "changed_bindings": record["changed_bindings"],
        "activation_required": True,
    }
