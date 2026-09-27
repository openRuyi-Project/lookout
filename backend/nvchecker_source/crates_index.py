"""Non-yanked versions from the Cargo sparse index; selection stays in nvchecker."""
import json

from nvchecker.api import GetVersionError, session

from tracker.identity import request_url


async def _versions(url):
    response = await session.get(url)
    versions = []
    for line in response.body.splitlines():
        try:
            record = json.loads(line)
            version, yanked = record["vers"], record["yanked"]
            if not isinstance(version, str) or not version or type(yanked) is not bool:
                raise ValueError()
        except (ValueError, KeyError, TypeError) as error:
            raise GetVersionError("invalid Cargo index record") from error
        if not yanked:
            versions.append(version)
    return versions


async def get_version(name, conf, *, cache, **kwargs):
    try:
        url = request_url(conf)
    except ValueError as error:
        raise GetVersionError("invalid Cargo index source") from error
    # One cached response serves multiple configured maintenance lines.
    return list(await cache.get(url, _versions))
