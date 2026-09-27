"""Reviewed crates.io identity and cached registry metadata, not name inference."""
import re
from urllib.parse import quote

from tracker.identity import from_package
from tracker.providers.model import Release

HOSTS = {"crates.io"}


def project(settings):
    if (not isinstance(settings, dict) or set(settings) != {"cratesio"}
            or not isinstance(settings["cratesio"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", settings["cratesio"])):
        raise ValueError("monitor requires a crates.io identity")
    return settings["cratesio"]


def inputs(package, configured):
    if configured is not None:
        project(configured)
        return configured
    identity = from_package(package)
    if identity and identity["ecosystem"] == "crates.io":
        result = {"cratesio": identity["name"]}
        project(result)
        return result
    return None


def release(name, version, io):
    """One project fetch serves all its versions and all three domain monitors."""
    if not isinstance(version, str) or not 1 <= len(version) <= 512:
        raise ValueError("monitor requires an observed release version")
    project({"cratesio": name})
    url = f"https://crates.io/api/v1/crates/{quote(name, safe='')}"
    # The registry API permits at most one request/second. IO applies this across
    # workers and domain monitors, after the shared cache lookup.
    data = io.json("GET", url, min_interval=1.0)
    if (not isinstance(data, dict) or not isinstance(data.get("crate"), dict)
            or not isinstance(data.get("versions"), list)
            or str(data["crate"].get("id", "")).lower() != name.lower()):
        raise ValueError("crates.io response identity does not match the query")
    matches = [item for item in data["versions"]
               if isinstance(item, dict) and item.get("num") == version]
    if len(matches) != 1 or str(matches[0].get("crate", "")).lower() != name.lower():
        raise ValueError("crates.io did not return the exact release identity")
    return matches[0], url


def metadata(settings, version, io):
    name = project(settings)
    info, url = release(name, version, io)
    declaration = info.get("license")
    # Cargo's deprecated slash means OR. Keep the original declaration as evidence.
    expression = declaration.replace("/", " OR ") if isinstance(declaration, str) else declaration
    return Release(
        name=name, source="crates.io", url=url,
        license_expression=expression, license_declaration=declaration,
        yanked=info.get("yanked"), yanked_reason=None,
    )
