"""Identity and release transport shared by independent PyPI monitors."""

from urllib.parse import quote

from .package_identity import from_package


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
    return data["info"], url
