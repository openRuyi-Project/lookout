"""Provider boundary: source metadata, not SPEC/RPM or host resolution."""

from copy import deepcopy

import pytest
from packaging.markers import Marker
from packaging.specifiers import SpecifierSet

from tracker import requires_pypi
from tracker.requirements import RequirementDeclaration, UnsupportedRequirements


URL = "https://pypi.org/pypi/upstream-fixture/2.0/json"


class MetadataIO:
    def __init__(self, info):
        self.info = info
        self.calls = []

    def json(self, method, url):
        self.calls.append((method, url))
        return {"info": deepcopy(self.info)}


def read(info):
    io = MetadataIO(info)
    result = requires_pypi.read("2.0", {"pypi": "upstream-fixture"}, io)
    assert io.calls == [("GET", URL)]
    return result


def test_python_only_compatibility_preserves_literal_declaration_and_provenance():
    item, = read({"requires_python": " >=3.8 "})
    assert (item.dependency, item.name, item.kind, item.scheme) == ("python", "Python", "runtime", "pep440")
    assert item.declaration == " >=3.8 "
    assert item.comparison == SpecifierSet(">=3.8")
    assert (item.source, item.url) == ("PyPI", URL)
    assert item.identity is None and item.condition is None and item.extras == ()
    assert item.optional is False


def test_distributions_have_registry_identity_and_normalized_constraints():
    python, package, unversioned = read({
        "requires_python": ">=3.9",
        "requires_dist": ["Upstream_Package >= 1.0, < 3", "unbounded-package"],
        # These unrelated provider fields must never become dependency facts.
        "requires": ["rpm-library"], "build_requires": ["build-only-tool"],
    })
    assert python.dependency == "python"
    assert (package.dependency, package.name) == ("pypi.upstream-package", "Upstream_Package")
    assert package.identity == {"ecosystem": "PyPI", "name": "upstream-package"}
    assert (package.kind, package.scheme, package.declaration) == ("runtime", "pep440", "<3,>=1.0")
    assert package.comparison == SpecifierSet("<3,>=1.0")
    assert package.source == "PyPI" and package.url == URL
    assert unversioned.declaration == "" and unversioned.comparison == SpecifierSet("")


def test_conditional_and_extra_clauses_remain_observations_not_host_evaluations(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("provider must not evaluate the host or requested extras")

    monkeypatch.setattr(Marker, "evaluate", forbidden)
    monkeypatch.setattr("packaging.markers.default_environment", forbidden)
    first, second, optional = read({"requires_dist": [
        'shared[security,fast]>=2; python_version < "3.10"',
        'shared>=3; python_version >= "3.10"',
        'optional-helper; extra == "docs" and sys_platform == "win32"',
    ]})
    assert first.dependency == second.dependency == "pypi.shared"
    assert first.extras == ("fast", "security") and second.extras == ()
    assert first.condition == 'python_version < "3.10"'
    assert second.condition == 'python_version >= "3.10"'
    assert optional.condition == 'extra == "docs" and sys_platform == "win32"'
    assert optional.declaration == ""
    assert first.optional is second.optional is False
    assert optional.optional is True


@pytest.mark.parametrize('marker,optional', [
    (None, False),
    ('sys_platform == "linux"', False),
    ('extra == "speedups"', True),
    ('"speedups" == extra', True),
    ('(platform_python_implementation == "CPython" and sys_platform != "android" '
     'and sys_platform != "ios") and extra == "speedups"', True),
    ('extra == "speedups" or extra == "docs"', True),
    ('extra == "speedups" and (sys_platform == "linux" or sys_platform == "win32")', True),
    ('extra == "speedups" or sys_platform == "linux"', False),
    ('sys_platform == "linux" or extra == "speedups" and python_version >= "3"', False),
    ('extra != "speedups"', False),
    ('extra == ""', False),
    ('extra != ""', True),
    ('extra in "speedups,docs"', None),
    ('extra >= "speedups"', None),
    ('extra in "speedups,docs" and extra == "docs"', True),
    ('extra in "speedups,docs" or sys_platform == "linux"', False),
])
def test_optionality_is_feature_gating_not_platform_applicability(marker, optional, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('classification must not evaluate host or target environment')

    monkeypatch.setattr(Marker, 'evaluate', forbidden)
    monkeypatch.setattr('packaging.markers.default_environment', forbidden)
    expression = 'helper[secure]>=1' + ('; ' + marker if marker else '')
    item, = read({'requires_dist': [expression]})
    assert item.optional is optional
    assert item.extras == ('secure',)  # Dependency extras do not opt the parent into a feature.
    assert item.condition == (str(Marker(marker)) if marker else None)
    assert RequirementDeclaration.model_validate(item.fact()).optional is optional


def test_unknown_private_marker_shape_is_unknown_not_a_provider_failure():
    from types import SimpleNamespace

    assert requires_pypi._optional(SimpleNamespace(_markers=['future syntax'])) is None
    assert requires_pypi._optional(SimpleNamespace()) is None


def test_marker_classification_has_a_recursion_budget():
    from types import SimpleNamespace

    tree = Marker('extra == "speedups"')._markers
    for _ in range(33):
        tree = [tree]
    assert requires_pypi._optional(SimpleNamespace(_markers=tree)) is None


def test_legacy_condition_does_not_become_required_by_default():
    item, = read({'requires_dist': ['helper; extra == "speedups"']})
    old = item.fact()
    del old['optional']
    assert RequirementDeclaration.model_validate(old).optional is None


def test_normalized_specifiers_compare_equally_despite_order_and_equivalent_versions():
    first, = read({"requires_dist": ["pkg>=1.0,<3"]})
    second, = read({"requires_dist": ["pkg<3,>=1"]})
    assert first.comparison == second.comparison


@pytest.mark.parametrize("info", [
    {}, {"requires_python": None}, {"requires_python": ""}, {"requires_python": " "},
    {"requires_dist": None}, {"requires_dist": []},
    {"requires_python": None, "requires_dist": []},
])
def test_missing_runtime_metadata_never_means_observed_empty_requirement_set(info):
    with pytest.raises(UnsupportedRequirements, match="missing"):
        read(info)


@pytest.mark.parametrize("python", [None, "", " "])
def test_distribution_facts_do_not_depend_on_python_constraint_availability(python):
    item, = read({"requires_python": python, "requires_dist": ["upstream-library>=2"]})
    assert item.dependency == "pypi.upstream-library"


@pytest.mark.parametrize("dist", [None, []])
def test_absent_dist_metadata_does_not_hide_observed_python_fact(dist):
    item, = read({"requires_python": ">=3.8", "requires_dist": dist})
    assert item.dependency == "python"


@pytest.mark.parametrize("dist", [False, {}, "pkg>=1", [None], [1], [""], [" "],
    ["pkg=>2"], ['pkg>=1; imaginary_environment == "x"'], ["pkg>=1", "pkg=>2"]])
def test_any_invalid_dist_input_is_unsupported_without_successful_partial_list(dist):
    with pytest.raises(UnsupportedRequirements, match="Requires-Dist"):
        read({"requires_python": ">=3.8", "requires_dist": dist})


@pytest.mark.parametrize("python", [False, 3, "Python 3", "x" * 1025, " " * 1025, ">=3.8," * 65])
def test_invalid_python_constraint_cannot_be_hidden_by_distribution_facts(python):
    with pytest.raises(UnsupportedRequirements, match="Requires-Python"):
        read({"requires_python": python, "requires_dist": ["valid>=1"]})


@pytest.mark.parametrize("declaration", [
    "package @ https://example.invalid/package.whl",
    "package @ file:///nonexistent/local/package.whl",
    "package @ git+https://example.invalid/repo.git",
])
def test_direct_urls_are_explicitly_unsupported_and_never_fetched(declaration):
    io = MetadataIO({"requires_dist": [declaration]})
    with pytest.raises(UnsupportedRequirements, match="direct URL"):
        requires_pypi.read("2.0", {"pypi": "upstream-fixture"}, io)
    assert io.calls == [("GET", URL)]


@pytest.mark.parametrize("declarations", [
    ["pkg"] * 257,
    ["p" * 1025],
    ["p" * 96],
    ["pkg" + ",".join(">=" + str(i) for i in range(65))],
    ["pkg[" + ",".join("e" + str(i) for i in range(65)) + "]"],
    ["pkg[" + "e" * 101 + "]"],
])
def test_comparison_budget_fails_instead_of_truncating_metadata(declarations):
    with pytest.raises(UnsupportedRequirements, match="budget"):
        read({"requires_dist": declarations})


def test_entire_bounded_list_is_parsed_in_source_order():
    result = read({"requires_dist": ["package-" + str(i) for i in range(256)]})
    assert len(result) == 256
    assert [item.dependency for item in result] == ["pypi.package-" + str(i) for i in range(256)]
    assert all(item.kind == "runtime" and item.declaration == "" for item in result)


def test_repeated_identical_clause_is_one_observation():
    item, = read({"requires_dist": ["Pkg>=1", "pkg>=1", "PKG>=1"]})
    assert item.dependency == "pypi.pkg" and item.name == "Pkg"
    assert item.declaration == ">=1" and item.comparison == SpecifierSet(">=1")


def test_repeated_same_conditional_clause_combines_all_constraints():
    item, = read({"requires_dist": [
        'Pkg[fast,security]>=1; python_version < "3.10"',
        'pkg[security,fast]<3; python_version < "3.10"',
        'PKG[security,fast]; python_version < "3.10"',
    ]})
    assert item.dependency == "pypi.pkg" and item.name == "Pkg"
    assert item.identity == {"ecosystem": "PyPI", "name": "pkg"}
    assert item.declaration == "<3,>=1" and item.comparison == SpecifierSet(">=1,<3")
    assert item.condition == 'python_version < "3.10"' and item.extras == ("fast", "security")
    assert item.source == "PyPI" and item.url == URL


def test_distinct_markers_and_extras_are_not_merged():
    result = read({"requires_dist": [
        "pkg>=1", "pkg[fast]>=2", 'pkg>=3; extra == "docs"', 'pkg>=4; extra == "test"',
    ]})
    assert [item.declaration for item in result] == [">=1", ">=2", ">=3", ">=4"]
    assert [item.extras for item in result] == [(), ("fast",), (), ()]
    assert [item.condition for item in result] == [None, None, 'extra == "docs"', 'extra == "test"']


def test_combined_clause_has_the_same_comparison_budget_as_single_clause():
    with pytest.raises(UnsupportedRequirements, match="Combined.*budget"):
        read({"requires_dist": ["pkg>=" + str(i) for i in range(65)]})
