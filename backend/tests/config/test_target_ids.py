"""Configured target identities remain valid through query serialization."""
import pytest
import tomlkit
from urllib.parse import parse_qsl

from tracker import config
from tracker.readmodel.query import FilterQuery, encode_parameters


@pytest.mark.parametrize('identity', ['a' * 94, 'rva23', 'other_x86_64', 'a+b', '目标'])
def test_loaded_target_roundtrips(configured_path, identity):
    doc = tomlkit.parse(configured_path.read_text())
    doc['targets'][0]['id'] = identity
    configured_path.write_text(tomlkit.dumps(doc))
    loaded = config.load(configured_path)
    query = FilterQuery(selections={'build:' + loaded['targets'][0]['id']: ('failed',)})
    assert FilterQuery.extract(parse_qsl(encode_parameters(query.parameters())))[0] == query


@pytest.mark.parametrize('identity', ['', 'a' * 95, 'a=b', 42, True])
def test_invalid_target_fails_at_load(configured_path, identity):
    doc = tomlkit.parse(configured_path.read_text())
    doc['targets'][0]['id'] = identity
    configured_path.write_text(tomlkit.dumps(doc))
    with pytest.raises(ValueError):
        config.load(configured_path)


def test_adapter_facet_cannot_embed_wire_separator():
    with pytest.raises(ValueError):
        FilterQuery(selections={'provider=other': ('value',)})
