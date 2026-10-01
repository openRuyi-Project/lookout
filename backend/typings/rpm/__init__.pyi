# SPDX-License-Identifier: MulanPSL-2.0
# Used RPM C-extension surface; the native gate checks it against the installed bindings.
__version__: str
RPMTAG_NAME: int
RPMTAG_VERSION: int
RPMTAG_SUMMARY: int
RPMTAG_LICENSE: int
RPMTAG_URL: int
RPMTAG_DESCRIPTION: int
RPMBUILD_ISSOURCE: int

def reloadConfig() -> None: ...
def addMacro(name: str, body: str) -> None: ...
def expandMacro(expression: str) -> str: ...
def labelCompare(left: tuple[str, str, str], right: tuple[str, str, str]) -> int: ...

class hdr:
    def __getitem__(self, tag: int) -> str | bytes | None: ...

class spec:
    def __init__(self, path: str) -> None: ...
    sourceHeader: hdr
    sources: list[tuple[str, int, int]]
    parsed: str
