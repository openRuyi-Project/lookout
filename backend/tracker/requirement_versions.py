"""Constraint syntaxes, independent of dependency names and metadata providers.

An unknown syntax or unrepresentable observed version stays unknown. These
comparisons cover version constraints only, not installed RPM capabilities.
"""
import operator
import re

from packaging.specifiers import SpecifierSet
from packaging.version import Version


def pep440(expression, version):
    return SpecifierSet(expression).contains(Version(version))


def numeric_release(version):
    """One to three numeric components, padded with zeros; no RPM suffix guess."""
    if not isinstance(version, str) or not re.fullmatch(r'(0|[1-9][0-9]{0,8})(\.(0|[1-9][0-9]{0,8})){0,2}', version):
        raise ValueError('not a numeric release')
    parts = tuple(map(int, version.split('.')))
    return parts + (0,) * (3 - len(parts))


def numeric_minimum(expression, version):
    return numeric_release(version) >= numeric_release(expression)


def rpm_version(expression, version):
    """Compare RPM VERSION, explicitly excluding unobserved Epoch/Release."""
    import rpm

    match = re.fullmatch(r'\s*(>=|<=|!=|==|=|>|<)\s*([A-Za-z0-9._+~^]+)\s*', expression)
    if not match or not re.fullmatch(r'[A-Za-z0-9._+~^]+', version):
        raise ValueError('not an RPM VERSION constraint')
    operations = {'>=': operator.ge, '<=': operator.le, '!=': operator.ne,
                  '==': operator.eq, '=': operator.eq, '>': operator.gt, '<': operator.lt}
    order = rpm.labelCompare(('0', version, '0'), ('0', match[2], '0'))
    return operations[match[1]](order, 0)


# Trusted syntax implementations, never code or package-name rules from config.
COMPARATORS = {'pep440': pep440, 'rpm_version': rpm_version, 'numeric_minimum': numeric_minimum}


def satisfies(scheme, expression, version):
    compare = COMPARATORS.get(scheme)
    if compare is None:
        return None, 'unsupported_comparison'
    try:
        return compare(expression, version), None
    except (ValueError, ImportError):
        return None, 'unsupported_version'
