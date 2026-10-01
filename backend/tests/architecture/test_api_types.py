"""UI schemas are selected by reachability and compiled by the standard generator."""
from pathlib import Path
import runpy
from types import SimpleNamespace


def generator(schemas, response):
    module = runpy.run_path(str(Path(__file__).resolve().parents[3] / 'scripts/api-types.py'))
    document = {'openapi': '3.1.0', 'info': {'title': 'Fixture', 'version': '1'},
                'components': {'schemas': schemas},
                'paths': {'/api/ui/packages': {'get': {'responses': {'200': response}}}}}
    module['render'].__globals__['create_app'] = lambda: SimpleNamespace(openapi=lambda: document)
    return module, document


def test_ui_contract_follows_references_without_domain_payloads():
    module, document = generator({
        'Page': {'type': 'object', 'properties': {'child': {'$ref': '#/components/schemas/Child'}}},
        'Child': {'type': 'object', 'properties': {'parent': {'$ref': '#/components/schemas/Page'}}},
        'ProviderResult': {'type': 'string'},
    }, {'$ref': '#/components/schemas/Page'})
    selected = module['ui_contract'](document)
    assert set(selected['components']['schemas']) == {'Page', 'Child'}
    assert selected['paths'] == {}
    assert 'ProviderResult' in document['components']['schemas']
