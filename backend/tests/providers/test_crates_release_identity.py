import pytest

from tracker.providers.cratesio import metadata


class IO:
    def __init__(self, versions):
        self.versions = versions

    def json(self, *args, **kwargs):
        return {'crate': {'id': 'fixture'}, 'versions': [
            {'crate': 'fixture', 'num': version, 'license': 'MIT', 'yanked': False}
            for version in self.versions]}


def test_unique_registry_build_metadata_can_restore_normalized_version():
    result = metadata({'cratesio': 'fixture'}, '1.2.3', IO(['1.2.3+upstream.4', '1.2.3-rc.1']))
    assert result.version == '1.2.3+upstream.4'
    assert result.license_expression == 'MIT'


@pytest.mark.parametrize('versions', [
    ['1.2.3+a', '1.2.3+b'], ['1.2.3-rc.1'], ['1.2.4+a'],
])
def test_ambiguous_or_other_release_is_not_an_exact_identity(versions):
    with pytest.raises(ValueError, match='exact release'):
        metadata({'cratesio': 'fixture'}, '1.2.3', IO(versions))


def test_explicit_build_metadata_is_never_discarded():
    with pytest.raises(ValueError, match='exact release'):
        metadata({'cratesio': 'fixture'}, '1.2.3+specific', IO(['1.2.3+other']))
