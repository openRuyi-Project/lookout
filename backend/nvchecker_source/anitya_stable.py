"""The first stable version in Anitya's own project ordering, not a generic max."""
from nvchecker.api import GetVersionError

from tracker.identity import request_url


async def get_version(name, conf, *, cache, **kwargs):
    try:
        url = request_url(conf)
    except ValueError as error:
        raise GetVersionError("invalid Anitya stable source") from error
    data = await cache.get_json(url)
    versions = data.get("stable_versions") if isinstance(data, dict) else None
    if not isinstance(versions, list) or any(not isinstance(v, str) or not v for v in versions):
        raise GetVersionError("invalid Anitya stable history")
    # Return at most one value: nvchecker still owns prefix/normalization, but
    # cannot reorder history according to a different version scheme.
    return versions[:1]
