"""Render-test bridge: use the real Python presenter, not a JS copy of its rules."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tracker import presentation

request = json.load(sys.stdin)
operation = request['operation']
if operation == 'detail':
    result = presentation.detail(request['payload']).model_dump()
elif operation == 'list':
    result = presentation.listing(request['payload'], request['query']).model_dump()
elif operation == 'theme':
    result = presentation.theme(request['payload'])
else:
    raise ValueError(operation)
json.dump(result, sys.stdout)
