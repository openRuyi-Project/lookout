import pytest

from tracker.monitors import yanked
from tracker.providers import go
from tracker.providers.go_mod import retractions, version_key


class IO:
    def __init__(self, document, versions='v1.0.0\nv1.0.1\nv1.1.0-rc.1'):
        self.document, self.versions, self.urls = document, versions, []

    def text(self, url, **kwargs):
        self.urls.append(url)
        return self.versions if url.endswith('/list') else self.document

    def json(self, method, url, **kwargs):
        raise AssertionError('the version list suffices; do not use filtered @latest')


def test_self_retracted_highest_release_is_authoritative():
    io = IO('module example.org/m\nretract (\nv1.0.0 // old\nv1.0.1 // self\n)\n')
    result = yanked.check({'version': '1.0.0'}, {'go': 'example.org/m'}, io)
    assert result['status'] == 'ok'
    assert result['findings'][0]['facts'][0]['key'] == 'Release retracted'
    assert io.urls[-1].endswith('/v1.0.1.mod')


def test_ranges_comments_and_unrelated_blocks():
    ranges = retractions('''module example.org/m
// retract v0.0.1 is only a comment
require (
 example.org/dependency v2.0.0
)
retract [v1.0.0, v1.1.0] // inclusive
''', 'example.org/m')
    assert len(ranges) == 1
    assert ranges[0][0] <= version_key('v1.1.0') <= ranges[0][1]
    assert not ranges[0][0] <= version_key('v1.1.1') <= ranges[0][1]


@pytest.mark.parametrize('document', [
    'module other.org/m', 'module example.org/m\nretract (\nv1.0.0',
    'module example.org/m\nretract invalid',
    'module example.org/m\nretract [v2.0.0, v1.0.0]',
    'module "example.org/m', 'module example.org/m\nretract "v1.0.0',
    'module example.org/m\nretract [v1.0.0 v1.1.0]',
    'module example.org/m\n/* retract v1.0.0 */',
])
def test_unsupported_syntax_does_not_become_not_retracted(document):
    with pytest.raises(ValueError):
        retractions(document, 'example.org/m')


def test_go_semver_order_and_module_escape():
    assert version_key('v1.0.0-rc.2') < version_key('v1.0.0-rc.10') < version_key('v1.0.0')
    io = IO('module example.org/Upper')
    assert go.withdrawal({'go': 'example.org/Upper'}, '1.0.0', io).yanked is False
    assert '/!upper/' in io.urls[-1]


def test_unknown_current_release_is_verified_not_assumed():
    io = IO('module example.org/m')
    io.json = lambda *args, **kwargs: {'Version': 'v1.0.9'}
    with pytest.raises(ValueError, match='identity'):
        go.withdrawal({'go': 'example.org/m'}, '1.0.8', io)


@pytest.mark.parametrize('quote', ['"', '`', ''])
def test_quoted_tokens_and_unspaced_block_delimiters(quote):
    q = lambda value: quote + value + quote
    document = ('module ' + q('example.org/m') + '\nrequire ' + q('other.org/m') + ' v1.0.0\n'
                'retract(\n[' + q('v1.0.0') + ',' + q('v1.0.1') + ']// comment\n)\n')
    assert retractions(document, 'example.org/m') == [(version_key('v1.0.0'), version_key('v1.0.1'))]


def test_comment_delimiters_inside_strings_are_not_comments():
    with pytest.raises(ValueError):
        retractions('module "example.org/m//other"', 'example.org/m')
    with pytest.raises(ValueError):
        retractions('module "example.org/\\x6d"', 'example.org/m')


def test_incompatible_suffix_requires_proxy_evidence_for_the_exact_tag():
    io = IO('module example.org/m\nretract v2.0.0', 'v2.0.0+incompatible')
    calls = []
    def info(method, url, **kwargs):
        calls.append(url)
        return {'Version': 'v2.0.0+incompatible'}
    io.json = info
    result = go.withdrawal({'go': 'example.org/m'}, '2.0.0', io)
    assert result.yanked is True and result.version == 'v2.0.0+incompatible'
    assert calls[0].endswith('/v2.0.0.info')
    io.json = lambda *args, **kwargs: {'Version': 'v2.0.1+incompatible'}
    with pytest.raises(ValueError, match='identity'):
        go.withdrawal({'go': 'example.org/m'}, '2.0.0', io)
    io.json = lambda *args, **kwargs: {'Version': 'v1.0.0+incompatible'}
    with pytest.raises(ValueError, match='identity'):
        go.withdrawal({'go': 'example.org/m'}, '1.0.0', io)
