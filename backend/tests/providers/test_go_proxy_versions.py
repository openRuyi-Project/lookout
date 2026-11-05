import pytest

from tracker.providers import go


class IO:
    def __init__(self, current, versions):
        self.current, self.versions, self.urls = current, versions, []

    def json(self, method, url, **kwargs):
        self.urls.append(url)
        return {"Version": self.current}

    def text(self, url, **kwargs):
        self.urls.append(url)
        return self.versions if url.endswith("/list") else "module example.org/Upper\nretract v1.2.3-RC1"


@pytest.mark.parametrize("version,canonical,escaped", [
    ("1.2.3-RC1", "v1.2.3-RC1", "v1.2.3-!r!c1"),
    ("2.0.0-RC1", "v2.0.0-RC1+incompatible", "v2.0.0-!r!c1%2Bincompatible"),
    ("1.2.3-rc.1", "v1.2.3-rc.1", "v1.2.3-rc.1"),
    ("1.2.3", "v1.2.3", "v1.2.3"),
])
def test_module_document_escapes_proxy_versions(version, canonical, escaped):
    io = IO(canonical, "")
    document, url = go.module_document({"go": "example.org/Upper"}, version, io)
    base = "https://proxy.golang.org/example.org/!upper/@v/"
    requested = escaped.removesuffix("%2Bincompatible")
    assert io.urls == [base + requested + ".info", base + escaped + ".mod"]
    assert url == base + escaped + ".mod"
    assert document.startswith("module example.org/Upper")


@pytest.mark.parametrize("verify_current", [False, True])
@pytest.mark.parametrize("current,latest,current_path,latest_path", [
    ("v1.2.3-RC1", "v1.2.3-RC2", "v1.2.3-!r!c1", "v1.2.3-!r!c2"),
    ("v1.2.3-rc.1", "v1.2.3-rc.2", "v1.2.3-rc.1", "v1.2.3-rc.2"),
    ("v1.2.3", "v1.2.4", "v1.2.3", "v1.2.4"),
])
def test_withdrawal_escapes_proxy_versions(verify_current, current, latest, current_path, latest_path):
    versions = latest if verify_current else current + "\n" + latest
    io = IO(current, versions)
    result = go.withdrawal({"go": "example.org/Upper"}, current, io)
    base = "https://proxy.golang.org/example.org/!upper/"
    expected = [base + "@v/list"]
    if verify_current:
        expected.append(base + "@v/" + current_path + ".info")
    expected.append(base + "@v/" + latest_path + ".mod")
    assert io.urls == expected
    assert result.url == expected[-1]
    assert result.version == current
    assert result.yanked is (current == "v1.2.3-RC1")
