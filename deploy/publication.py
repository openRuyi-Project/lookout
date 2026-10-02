"""Discover tested GitHub publications without polling the image registry."""
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


_MAX_RESPONSE = 1024 * 1024


def channel_repository(reference):
    match = re.fullmatch(r'ghcr\.io/([a-z0-9_.-]+/[a-z0-9_.-]+):(main|latest)', reference)
    return match[1] if match else None


def published_image(reference, current_image, current_revision, workflow):
    repository = channel_repository(reference)
    if not repository or not re.fullmatch(r'[A-Za-z0-9_.-]+\.ya?ml', workflow):
        raise ValueError('publication checks require a GHCR main/latest channel and a workflow filename')
    if not re.fullmatch(r'[0-9a-f]{40}', current_revision or ''):
        raise ValueError('running image must declare its source revision')
    query = urlencode(dict(branch='main', status='success', per_page=1))
    url = f'https://api.github.com/repos/{repository}/actions/workflows/{quote(workflow, safe="")}/runs?{query}'
    request = Request(url, headers={'Accept': 'application/vnd.github+json',
                                  'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'Lookout-updater'})
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read(_MAX_RESPONSE + 1)
    except (HTTPError, URLError, TimeoutError) as error:
        # Discovery failure must never turn into a speculative registry pull.
        raise RuntimeError('GitHub publication check failed; service unchanged') from error
    if len(body) > _MAX_RESPONSE:
        raise ValueError('GitHub publication response exceeds the size budget')
    runs = json.loads(body)['workflow_runs']
    if not isinstance(runs, list):
        raise ValueError('GitHub publication response must contain a run list')
    if not runs:
        return current_image, current_revision
    run = runs[0]
    revision = run.get('head_sha')
    if (run.get('status') != 'completed' or run.get('conclusion') != 'success'
            or run.get('head_branch') != 'main' or run.get('event') not in ('push', 'workflow_dispatch')
            or run.get('head_repository', {}).get('full_name', '').lower() != repository
            or not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision)):
        raise ValueError('GitHub run does not identify a successful channel publication')
    return (current_image if revision == current_revision else f'ghcr.io/{repository}:sha-{revision}'), revision
