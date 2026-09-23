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


def plan(base_path, candidate_path, runtime_path, output, snapshot=None):
    base_file, _, base = inputs(base_path, snapshot)
    candidate_file, _, candidate = inputs(candidate_path, snapshot)
    runtime_file, native_file, runtime = inputs(runtime_path, snapshot)
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
    tracker_text = edit_tables(runtime_file.read_text(), binding_changes, ("packages",))
    tracker_text = edit_tables(tracker_text, appearance_changes, ("openruyi", "buildsystems"))
    tracker_text = edit_tables(tracker_text, monitor_changes)
    output_native = str(native_file.relative_to(runtime_file.parent))
    native_text = merge_text(
        Path(base["nvpath"]).read_text(), Path(candidate["nvpath"]).read_text(),
        native_file.read_text(), output_native,
    )
    texts = {output_native: native_text, runtime_file.name: tracker_text}
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
    expected_bindings = dict(runtime["packages"])
    for name, entry in binding_changes.items():
        if entry is None:
            expected_bindings.pop(name, None)
        else:
            expected_bindings[name] = entry
    if prepared_config["packages"] != expected_bindings:
        raise ValueError("text promotion differs from reviewed package bindings")
    if prepared_config["native"] != expected_native:
        raise ValueError("text promotion differs from reviewed effective rules")
    if prepared_config["native_options"] != runtime["native_options"]:
        raise ValueError("text promotion changed native operator options")
    record = {
        "schema": 1,
        "runtime_config": str(runtime_file),
        "baseline_hashes": baseline_hashes,
        "proposed_hashes": {n: digest(output / n) for n in texts},
        "inventory_generation": (snapshot or {}).get("generation"),
        "changed_tracks": sorted(native_changes),
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
    if record.get("schema") != 1 or str(runtime_file) != record["runtime_config"]:
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
        or proposed != names
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
