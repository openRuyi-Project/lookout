# SPDX-FileCopyrightText: (C) 2026 Institute of Software, Chinese Academy of Sciences (ISCAS)
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""Parser tests for the one-pass SPECS changelog source. git performs history
simplification and trailer extraction; these lock the bucketing of its
`git log --name-status` stream into per-package changelogs. Live equivalence with
per-package `git log` was cross-checked over the whole repo (0 mismatch, 2s)."""
import hashlib
import subprocess

import pytest

from tracker.monitors.source import git as sg


@pytest.mark.parametrize(('stderr', 'category'), [
    ('Could not resolve host: private.example', 'dns'),
    ('SSL certificate problem for https://secret@example.invalid', 'tls'),
    ('Failed to connect to internal.example port 22', 'connection'),
    ('Authentication failed for https://user:token@example.invalid', 'authentication'),
    ('The requested URL returned error: 503', 'http'),
    ('unexpected error includes private source path', None),
])
def test_git_failures_report_operation_and_category_without_private_details(monkeypatch, stderr, category):
    monkeypatch.setattr(sg.subprocess, 'run', lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 128, '', stderr))
    value, error = sg._git_text(['-C', '/private/secret/repo', 'fetch', 'origin'])
    suffix = f' ({category})' if category else ''
    assert value is None and error == 'git fetch exited 128' + suffix


def test_git_timeout_names_the_operation_not_a_path(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1)
    monkeypatch.setattr(sg.subprocess, 'run', timeout)
    assert sg._git_text(['-C', '/secret/repo', 'fetch']) == (None, 'git fetch timeout')


def test_local_sources_require_regular_pinned_blobs(monkeypatch):
    monkeypatch.setattr(sg, '_git_text', lambda *args: ('100644 blob abc\tSPECS/fixture/series\n', None))
    monkeypatch.setattr(sg, '_git_bytes', lambda *args: b'fixture')
    result = sg.read_local_sources('/repo', 'fixture', ['series'])
    assert result == [({'path': 'SPECS/fixture/series', 'name': 'series',
                        'sha256': hashlib.sha256(b'fixture').hexdigest()}, b'fixture')]
    monkeypatch.setattr(sg, '_git_text', lambda *args: ('120000 blob abc\tSPECS/fixture/series\n', None))
    with pytest.raises(ValueError, match='regular'):
        sg.read_local_sources('/repo', 'fixture', ['series'])

FS, GS, RS = sg.FS, sg.GS, sg.RS


def commit(h, subject, files, date='2026-01-01T00:00:00+00:00', author='A', sob=()):
    header = FS.join([h, date, author, subject, GS.join(sob)])
    status = '\n'.join(f'M\t{p}' for p in files)
    return RS + header + '\n' + status


def test_buckets_commit_into_touched_packages():
    stream = commit('c1', 'bump bash', ['SPECS/bash/bash.spec'], sob=('Kai <k@x>',))
    out = sg._bucket_log(stream, 20)
    assert set(out) == {'bash'}
    (e,) = out['bash']
    assert e['commit'] == 'c1' and e['subject'] == 'bump bash'
    assert e['signed_off_by'] == ['Kai <k@x>'] and e['date'].endswith('+00:00')


def test_one_commit_touching_many_packages():
    stream = commit('c1', 'tree-wide fix', ['SPECS/bash/bash.spec', 'SPECS/gcc/gcc.spec'])
    out = sg._bucket_log(stream, 20)
    assert set(out) == {'bash', 'gcc'}
    assert out['bash'][0]['commit'] == out['gcc'][0]['commit'] == 'c1'


def test_newest_first_and_limit_per_package():
    stream = (commit('c3', 's3', ['SPECS/bash/bash.spec'], date='2026-03-01T00:00:00+00:00')
              + commit('c2', 's2', ['SPECS/bash/bash.spec'], date='2026-02-01T00:00:00+00:00')
              + commit('c1', 's1', ['SPECS/bash/bash.spec'], date='2026-01-01T00:00:00+00:00'))
    out = sg._bucket_log(stream, 2)
    assert [e['commit'] for e in out['bash']] == ['c3', 'c2']   # log order preserved, capped at 2


def test_rename_status_takes_new_path():
    # git emits "R100\told\tnew" for renames; the new path names the package.
    header = FS.join(['c1', '2026-01-01T00:00:00+00:00', 'A', 'rename', ''])
    stream = RS + header + '\n' + 'R100\tSPECS/old/old.spec\tSPECS/newpkg/newpkg.spec'
    out = sg._bucket_log(stream, 20)
    assert 'newpkg' in out


def test_non_specs_paths_ignored_and_empty_stream():
    stream = commit('c1', 'docs', ['README.md', 'SPECS/bash/bash.spec'])
    out = sg._bucket_log(stream, 20)
    assert set(out) == {'bash'}
    assert sg._bucket_log('', 20) == {}


def test_malformed_header_skipped_not_guessed():
    bad = RS + 'onlyhash' + FS + 'two' + '\nM\tSPECS/bash/bash.spec'
    assert sg._bucket_log(bad, 20) == {}


def test_subject_with_colon_is_intact():
    stream = commit('c1', 'SPECS: bash: fix packaging: really', ['SPECS/bash/bash.spec'])
    assert sg._bucket_log(stream, 20)['bash'][0]['subject'] == 'SPECS: bash: fix packaging: really'


def test_unconfigured_macros_do_not_access_git(monkeypatch):
    monkeypatch.setattr(sg, '_git_text', lambda *_a, **_kw: pytest.fail('macros disabled'))
    assert sg.read_macros('/repo', None) == []


def test_macro_directory_without_macro_files_is_legitimately_empty(monkeypatch):
    monkeypatch.setattr(sg, '_git_text', lambda *_a, **_kw: ('README\npackage.spec\n', None))
    monkeypatch.setattr(sg, '_git_bytes', lambda *_a, **_kw: pytest.fail('no selected macro files'))
    assert sg.read_macros('/repo', 'package') == []


def test_macro_directory_failure_is_not_an_empty_set(monkeypatch):
    monkeypatch.setattr(sg, '_git_text', lambda *_a, **_kw: (None, 'git exited 128'))
    with pytest.raises(sg.MacroReadError, match='directory unavailable'):
        sg.read_macros('/repo', 'package')


@pytest.mark.parametrize('failed_blob', [None, b'x' * (sg._SIZE_LIMIT + 1)])
def test_unreadable_or_oversized_macro_never_returns_partial_set(monkeypatch, failed_blob):
    monkeypatch.setattr(sg, '_git_text', lambda *_a, **_kw: ('macros.a\nmacros.b\n', None))
    monkeypatch.setattr(sg, '_git_bytes', lambda args, *_a, **_kw:
                        b'%good 1\n' if args[-1].endswith('macros.a') else failed_blob)
    with pytest.raises(sg.MacroReadError):
        sg.read_macros('/repo', 'package')


def test_complete_macros_keep_deterministic_provenance(monkeypatch):
    monkeypatch.setattr(sg, '_git_text', lambda *_a, **_kw: ('macros.b\nREADME\nmacros.a\n', None))
    blobs = {'macros.a': b'%a 1\n', 'macros.b': b'%b 2\n'}
    monkeypatch.setattr(sg, '_git_bytes', lambda args, *_a, **_kw: blobs[args[-1].rsplit('/', 1)[1]])
    assert sg.read_macros('/repo', 'package') == [
        ({'path': 'SPECS/package/' + name, 'sha256': hashlib.sha256(data).hexdigest()}, data)
        for name, data in sorted(blobs.items())]
