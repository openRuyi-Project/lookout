"""Go proxy @latest releases, not pseudo-versions or prereleases."""
import re

from nvchecker.api import GetVersionError


async def get_version(name, conf, *, cache, **kwargs):
    data = await cache.get_json(conf["url"])
    if not isinstance(data, dict) or not isinstance(data.get("Version"), str):
        raise GetVersionError("invalid Go proxy version response")
    version = data["Version"]
    return [version] if re.fullmatch(r"v[0-9]+(?:\.[0-9]+){2}(?:\+incompatible)?", version) else []
