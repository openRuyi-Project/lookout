"""Run the real installer in a disposable home; never touch the user's units."""
from pathlib import Path
import os
import shutil
import shlex
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]

def test_installer_uses_configured_intervals(tmp_path):
    home=tmp_path/'home';base=home/'apps/openruyi-tracker';release=base/'releases/test'
    for p in ('frontend/dist/server','backend/tracker','config'):
        (release/p).mkdir(parents=True)
    (release/'frontend/dist/server/entry.mjs').touch()
    shutil.copy2(ROOT/'backend/tracker/config.py',release/'backend/tracker/config.py')
    shutil.copy2(ROOT/'backend/tracker/version_rules.py',release/'backend/tracker/version_rules.py')
    text=(ROOT/'config/tracker.toml').read_text().replace('obs_interval_seconds = 60','obs_interval_seconds = 37').replace('nvchecker_interval_seconds = 21600','nvchecker_interval_seconds = 7200')
    (release/'config/tracker.toml').write_text(text)
    shutil.copytree(ROOT/'config/versions',release/'config/versions')
    (base/'runtime/venv/bin').mkdir(parents=True)
    (base/'runtime/venv/bin/python').symlink_to(sys.executable)
    (base/'state').mkdir();(base/'state/tracker.sqlite3').touch()
    fake=tmp_path/'bin';fake.mkdir()
    for name in ('systemctl','systemd-analyze','curl'):
        p=fake/name;p.write_text('#!/bin/sh\nprintf "%s\\n" "'+name+' $*"\n');p.chmod(0o755)
    env={**os.environ,'HOME':str(home),'TRACKER_HOME':str(base),'PATH':str(fake)+':'+os.environ['PATH']}
    p=subprocess.run(['sh',str(ROOT/'deploy/install-user.sh'),str(release)],env=env,capture_output=True,text=True)
    print('INSTALL:',p.stdout,'STDERR:',p.stderr,'EXIT:',p.returncode)
    assert p.returncode==0
    units=home/'.config/systemd/user'
    assert 'OnUnitInactiveSec=37s' in (units/'openruyi-tracker-collect.timer').read_text()
    upstream=(units/'openruyi-tracker-upstreams.timer').read_text()
    assert 'OnUnitInactiveSec=7200s' in upstream and 'RandomizedDelaySec=5min' in upstream
    assert 'TimeoutStartSec=7500s' in (units/'openruyi-tracker-upstreams.service').read_text()
    assert (base/'current').resolve()==release
    # The same unit must still boot an older release during a native rollback.
    node=base/'runtime/node/bin/node';node.parent.mkdir(parents=True)
    node.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n');node.chmod(0o755)
    command=next(line.removeprefix('ExecStart=') for line in
                 (units/'openruyi-tracker-web.service').read_text().splitlines()
                 if line.startswith('ExecStart='))
    for launcher in ('dist/server/entry.mjs', 'server.mjs'):
        if launcher=='server.mjs': (release/'frontend/server.mjs').touch()
        result=subprocess.run(shlex.split(command),cwd=release/'frontend',text=True,capture_output=True)
        assert result.returncode==0 and result.stdout.strip()==launcher, result.stderr
    # No [spec] configured: the optional SPEC-git units must not be written.
    assert not (units/'openruyi-tracker-specs.timer').exists()
    assert not (units/'openruyi-tracker-specs.service').exists()
    before={f.name:f.read_bytes() for f in units.iterdir()}
    (release/'config/tracker.toml').write_text(text.replace('nvchecker_interval_seconds = 7200','nvchecker_interval_seconds = 0'))
    p=subprocess.run(['sh',str(ROOT/'deploy/install-user.sh'),str(release)],env=env,capture_output=True,text=True)
    assert p.returncode!=0 and 'positive integer' in p.stderr
    assert before=={f.name:f.read_bytes() for f in units.iterdir()}


def test_installer_writes_optional_spec_units_when_configured(tmp_path):
    home=tmp_path/'home';base=home/'apps/openruyi-tracker';release=base/'releases/test'
    for p in ('frontend/dist/server','backend/tracker','config'):
        (release/p).mkdir(parents=True)
    (release/'frontend/dist/server/entry.mjs').touch()
    shutil.copy2(ROOT/'backend/tracker/config.py',release/'backend/tracker/config.py')
    shutil.copy2(ROOT/'backend/tracker/version_rules.py',release/'backend/tracker/version_rules.py')
    text=(ROOT/'config/tracker.toml').read_text()
    text+='\n[spec]\nrepo = "'+str(tmp_path/'spec.git')+'"\nmacro_package = "rpm-config-openruyi"\nchangelog_limit = 20\ninterval_seconds = 9000\nfetch_timeout_seconds = 1200\n'
    (release/'config/tracker.toml').write_text(text)
    shutil.copytree(ROOT/'config/versions',release/'config/versions')
    (base/'runtime/venv/bin').mkdir(parents=True)
    (base/'runtime/venv/bin/python').symlink_to(sys.executable)
    (base/'state').mkdir();(base/'state/tracker.sqlite3').touch()
    fake=tmp_path/'bin';fake.mkdir()
    for name in ('systemctl','systemd-analyze','curl'):
        p=fake/name;p.write_text('#!/bin/sh\nprintf "%s\\n" "'+name+' $*"\n');p.chmod(0o755)
    env={**os.environ,'HOME':str(home),'TRACKER_HOME':str(base),'PATH':str(fake)+':'+os.environ['PATH']}
    p=subprocess.run(['sh',str(ROOT/'deploy/install-user.sh'),str(release)],env=env,capture_output=True,text=True)
    print('INSTALL:',p.stdout,'STDERR:',p.stderr,'EXIT:',p.returncode)
    assert p.returncode==0
    units=home/'.config/systemd/user'
    timer=(units/'openruyi-tracker-specs.timer').read_text()
    assert 'OnUnitInactiveSec=9000s' in timer and 'RandomizedDelaySec=5min' in timer
    service=(units/'openruyi-tracker-specs.service').read_text()
    assert 'TimeoutStartSec=1500s' in service and '--only specs' in service
