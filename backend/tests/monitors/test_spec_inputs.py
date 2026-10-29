# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""Input budgets are enforced before starting the confined RPM worker."""

import hashlib

import pytest

from tracker.monitors.source import rpm


def macro(data=b'%fixture 1\n'):
    return {'path': 'fixture/macros', 'sha256': hashlib.sha256(data).hexdigest()}, data


def test_macro_set_larger_than_sixteen_reaches_worker(monkeypatch):
    macros = [macro(f'%fixture_{i} {i}\n'.encode()) for i in range(17)]
    seen = []

    def run(inputs, work):
        seen.extend(path.read_bytes() for path in sorted(inputs.glob('macros.*')))
        return None, 'native SPEC parse failed'

    monkeypatch.setattr(rpm, '_run', run)
    result = rpm.query(b'Name: sample\n', macros)
    assert seen == [data for _, data in macros]
    assert result['native_query']['context']['additional_macros'] == [p for p, _ in macros]


def test_macro_count_budget_accepts_boundary():
    rpm.validate_macros([macro()] * rpm.MACRO_FILE_LIMIT)


def test_macro_count_budget_rejects_overflow():
    with pytest.raises(rpm.MacroInputError, match='too many native macro files'):
        rpm.validate_macros([macro()] * (rpm.MACRO_FILE_LIMIT + 1))


def test_macro_byte_budget_accepts_boundary():
    chunk = b'#' * rpm._SIZE_LIMIT
    rpm.validate_macros([macro(chunk)] * (rpm.MACRO_BYTES_LIMIT // len(chunk)))


def test_macro_byte_budget_rejects_overflow():
    chunk = b'#' * rpm._SIZE_LIMIT
    macros = [macro(chunk)] * (rpm.MACRO_BYTES_LIMIT // len(chunk)) + [macro(b'#')]
    with pytest.raises(rpm.MacroInputError, match='native macro inputs exceed total size limit'):
        rpm.validate_macros(macros)


def test_macro_file_size_is_independently_bounded():
    with pytest.raises(rpm.MacroInputError, match='native macro file exceeds size limit'):
        rpm.validate_macros([macro(b'#' * (rpm._SIZE_LIMIT + 1))])


def test_macro_hash_must_match():
    provenance, data = macro()
    with pytest.raises(rpm.MacroInputError, match='pinned hash'):
        rpm.validate_macros([(provenance, data + b'#')])


def test_query_applies_shared_validation_before_worker(monkeypatch):
    monkeypatch.setattr(rpm, '_run', lambda *args: pytest.fail('worker must not start'))
    with pytest.raises(rpm.MacroInputError):
        rpm.query(b'Name: sample\n', [macro()] * (rpm.MACRO_FILE_LIMIT + 1))


@pytest.mark.parametrize(('spec', 'sources', 'message'), [
    (b'#' * (rpm._SIZE_LIMIT + 1), (), 'SPEC exceeds size limit'),
    (b'', [(dict(name='input', sha256=hashlib.sha256(b'').hexdigest()), b'')] * 17,
     'too many local SPEC sources'),
    (b'', [(dict(name='../input', sha256=hashlib.sha256(b'').hexdigest()), b'')],
     'unique filename'),
    (b'', [(dict(name='input', sha256='wrong'), b'')], 'pinned hash'),
])
def test_spec_input_errors_are_distinct_from_shared_macro_errors(spec, sources, message):
    with pytest.raises(rpm.SpecInputError, match=message) as failure:
        rpm._parse(spec, (), sources)
    assert not isinstance(failure.value, rpm.MacroInputError)
