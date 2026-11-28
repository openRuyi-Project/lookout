"""Re-enter the production publisher after a registry write when the acknowledgement is lost."""
import os
from pathlib import Path
import subprocess
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[3]

# The registry double persists tags before acknowledging each write.
REGISTRY = """#!/bin/sh
set -eu
case "$1" in
inspect)
  file="$RUNNER_TEMP/${3##*:}"
  test -f "$file" || { echo 'manifest unknown' >&2; exit 1; }
  cat "$file" ;;
manifest-digest) cat "$2" ;;
copy)
  echo "${3##*@}" > "$RUNNER_TEMP/${4##*:}"
  if test "${4##*:}" = "$CUT"; then
    exit 97
  fi ;;
*) exit 2 ;;
esac
"""


@pytest.mark.parametrize('cut', ['sha', 'receipt', 'v1.0.1', 'main', 'latest', 'public'])
def test_publish_resumes_without_replacing_version_or_rebuilding(tmp_path, cut):
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=tmp_path, text=True).strip()

    git('init', '--bare', str(tmp_path / 'remote'))
    git('init')
    git('remote', 'add', 'origin', str(tmp_path / 'remote'))
    git('-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
        'commit', '--allow-empty', '-m', 'source')
    sha = git('rev-parse', 'HEAD')
    digest = 'sha256:' + 'a' * 64
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path), VERSION='1.0.1', ORIGINAL='42',
               CUT=cut, REAL_GIT=shutil.which('git'), BUMP='patch', GITHUB_SHA=sha, GITHUB_SERVER_URL='https://github.com',
               GITHUB_REPOSITORY='owner/repo', ID=digest,
               PATH=str(tmp_path) + os.pathsep + os.environ['PATH'])
    doubles = {'git': """#!/bin/sh
set -e
"$REAL_GIT" "$@"
if test "$1" = push && test "$CUT" = receipt; then exit 97; fi
""", 'skopeo': REGISTRY, 'jq': '#!/bin/sh\necho "$ID"\n',
               'docker': '''#!/bin/sh
case "$*" in
  *org.opencontainers.image.revision*) echo "$GITHUB_SHA" ;;
  *org.opencontainers.image.version*) echo "$VERSION" ;;
  *org.opencontainers.image.source*) echo "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY" ;;
  *'{{.Id}}'*) echo "$ID" ;;
  push*) echo "$ID" > "$RUNNER_TEMP/sha-$GITHUB_SHA"; if test "$CUT" = sha; then exit 97; fi ;;
  load*|tag*) ;;
  *) exit 2 ;;
esac
''',
               'gh': '''#!/bin/sh
case "$1 $2" in
  "api --paginate") if test -f "$RUNNER_TEMP/public"; then echo public; fi ;;
  "release create") test ! -f "$RUNNER_TEMP/public" && touch "$RUNNER_TEMP/public" || exit 1; if test "$CUT" = public; then exit 97; fi ;;
  *) exit 2 ;;
esac
'''}
    for name, contents in doubles.items():
        path = tmp_path / name
        path.write_text(contents)
        path.chmod(0o755)

    def publish():
        return subprocess.run(['bash', ROOT / 'scripts/publish-release.sh'],
                              cwd=tmp_path, env=env, capture_output=True, text=True)

    assert publish().returncode == 97
    env['CUT'] = ''
    # A retry must discover the receipt from the remote, not a prior local tag.
    if cut != 'sha':
        git('tag', '-d', 'v1.0.1')
    for _ in range(2):
        retry = publish()
        assert retry.returncode == 0, retry.stderr
        assert (tmp_path / 'public').exists()
        assert (tmp_path / 'latest').read_text().strip() == digest
    (tmp_path / 'v1.0.1').write_text('conflicting')
    assert publish().returncode != 0 and (tmp_path / 'v1.0.1').read_text() == 'conflicting'
    git('tag', 'v2.0.0')
    (tmp_path / 'main').write_text('newer')
    assert publish().returncode != 0 and (tmp_path / 'main').read_text() == 'newer'
