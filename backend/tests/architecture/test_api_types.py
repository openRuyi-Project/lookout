"""OpenAPI schema references and declarations must name the same TS types."""
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest


def generator(names):
    module = runpy.run_path(str(Path(__file__).resolve().parents[3] / 'scripts/api-types.py'))
    schema = {'anyOf': [{'$ref': '#/components/schemas/' + name} for name in names]}
    document = {'components': {'schemas': {name: {'type': 'string'} for name in names}},
                'paths': {'/api/ui/packages': {'get': {'responses': {'200': schema}}}}}
    module['render'].__globals__['create_app'] = lambda: SimpleNamespace(openapi=lambda: document)
    return module


def test_schema_names_have_valid_consistent_typescript_identifiers():
    module = generator(['FilterQuery-Input', 'FilterQuery-Output'])
    assert module['ts']({'$ref': '#/components/schemas/FilterQuery-Output'}) == 'FilterQuery_Output'
    assert module['render']().endswith(
        'export type FilterQuery_Input = string;\nexport type FilterQuery_Output = string;\n')


def test_name_normalization_rejects_collisions():
    with pytest.raises(ValueError, match='collide'):
        generator(['A-B', 'A_B'])['render']()
