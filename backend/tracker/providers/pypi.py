"""Identity and release transport shared by independent PyPI monitors."""

from urllib.parse import quote

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from tracker.identity import from_package
from tracker.providers.model import Release


HOSTS = {"pypi.org"}


def project(settings):
    if (not isinstance(settings, dict) or set(settings) != {"pypi"}
            or not isinstance(settings["pypi"], str)
            or not 1 <= len(settings["pypi"]) <= 512):
        raise ValueError("monitor requires a PyPI identity")
    return settings["pypi"]


def inputs(package, configured):
    if configured is not None:
        project(configured)
        return configured
    identity = from_package(package)
    return {"pypi": identity["name"]} if identity and identity["ecosystem"] == "PyPI" else None


def release(name, version, io):
    """Return release info and its evidence URL; shared IO owns request caching."""
    if not isinstance(version, str) or not 1 <= len(version) <= 512:
        raise ValueError("monitor requires an observed release version")
    url = f"https://pypi.org/pypi/{quote(name, safe='')}/{quote(version, safe='')}/json"
    data = io.json("GET", url)
    if not isinstance(data, dict) or not isinstance(data.get("info"), dict):
        raise ValueError("PyPI release response has no metadata object")
    info = data["info"]
    observed_name, observed_version = info.get("name"), info.get("version")
    if (not isinstance(observed_name, str) or not 1 <= len(observed_name) <= 512
            or canonicalize_name(observed_name) != canonicalize_name(name)
            or not isinstance(observed_version, str) or not 1 <= len(observed_version) <= 512):
        raise ValueError("PyPI response identity does not match the query")
    if observed_version != version:
        try:
            same_version = Version(observed_version) == Version(version)
        except InvalidVersion:
            same_version = False
        if not same_version:
            raise ValueError("PyPI response identity does not match the query")
    return info, url


def metadata(settings, version, io):
    name = project(settings)
    info, url = release(name, version, io)
    expression = info.get("license_expression")
    return Release(
        name=name, source="PyPI", url=url,
        license_expression=expression, license_declaration=expression,
        yanked=info.get("yanked"), yanked_reason=info.get("yanked_reason"),
    )
