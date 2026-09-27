"""Release policy fixtures, deliberately unrelated to today's latest versions."""
from pathlib import Path
import re
import tomllib

import pytest

RULES = tomllib.loads((Path(__file__).resolve().parents[3] / 'config/versions/nvchecker.toml').read_text())


def matches(name, filename):
    rule = RULES[name]
    versions = re.findall(rule['regex'], f'<a href="{filename}">download</a>')
    return [v for v in versions if re.fullmatch(rule.get('include_regex', '.*'), v)]


@pytest.mark.parametrize('name,component,version', [
    ('iw', 'iw', '999.2'),
    ('xcb-util', 'xcb-util', '999.2'),
    ('libotf', 'libotf', '999.2'),
    ('speex', 'speex', '999.2'),
    ('usbutils', 'usbutils', '999'),
])
def test_directory_rules_match_component_archives_not_siblings_or_signatures(name, component, version):
    filename = f'{component}-{version}.tar.xz'
    assert matches(name, filename) == [version]
    assert matches(name, '/releases/' + filename) == [version]
    assert matches(name, filename + '.asc') == []
    assert matches(name, f'{component}-tools-{version}.tar.xz') == []
    assert matches(name, f'{component}-{version}-rc1.tar.xz') == []
    assert matches(name, f'{component}-{version}beta1.tar.xz') == []


def test_dbus_stable_branches_and_release_micro_versions():
    assert matches('dbus', 'dbus-9.42.8.tar.xz') == ['9.42.8']
    assert matches('dbus', 'dbus-9.43.8.tar.xz') == []
    assert matches('dbus', 'dbus-9.42.9.tar.xz') == []


def test_python_binding_does_not_inherit_libdbus_branch_policy():
    assert matches('python-dbus-python', 'dbus-python-9.43.8.tar.xz') == ['9.43.8']
    assert matches('python-dbus-python', 'dbus-python-9.43.9.tar.xz') == []
