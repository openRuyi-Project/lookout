"""Render-test bridge: use the real Python presenter, not a JS copy of its rules."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tracker.presentation import pages as presentation_pages
from tracker.monitors.requires.model import RequirementAssessment

request = json.load(sys.stdin)
operation = request['operation']
packages = ([request['payload']] if operation == 'detail' else
            request['payload'].get('items', []) if operation == 'list' else [])
for package in packages:
    for monitor in package['monitors'].values():
        if monitor['data']['kind'] == 'requires':
            # SSR fixtures must supply the actual current/target assessment contract.
            monitor['data']['requirements'] = [RequirementAssessment.model_validate(item).model_dump()
                                               for item in monitor['data']['requirements']]
if operation == 'detail':
    result = presentation_pages.detail(request['payload']).model_dump()
elif operation == 'list':
    result = presentation_pages.listing(request['payload'], request['query']).model_dump()
elif operation == 'theme':
    result = presentation_pages.theme(request['payload'])
else:
    raise ValueError(operation)
json.dump(result, sys.stdout)
