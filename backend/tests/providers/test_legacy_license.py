from tracker.monitors import license as monitor


class IO:
    def __init__(self, before, after):
        self.values = {'1': before, '2': after}

    def json(self, method, url):
        version = url.split('/')[-2]
        return {'info': {'name': 'fixture', 'version': version, **self.values[version]}}


def test_legacy_exact_spdx_keeps_field_provenance():
    result = monitor.check({'version': '1', 'target_version': '2'}, {'pypi': 'fixture'},
                           IO({'license': 'MIT'}, {'license_expression': 'BSD-3-Clause'}))
    assert result['status'] == 'ok'
    facts = result['findings'][0]['facts']
    assert facts[0]['key'].endswith(' · license')
    assert facts[1]['key'].endswith(' · license_expression')
    assert facts[0]['value'] == 'MIT'


def test_invalid_modern_metadata_does_not_fall_back_to_legacy_or_classifiers():
    for info in ({'license': 'See LICENSE file'},
                 {'license_expression': 'not SPDX', 'license': 'MIT'},
                 {'classifiers': ['License :: OSI Approved :: MIT License']}):
        result = monitor.check({'version': '1', 'target_version': '2'}, {'pypi': 'fixture'}, IO(info, {'license': 'MIT'}))
        assert result['status'] == 'unsupported' and result['findings'] == []
