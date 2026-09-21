#!/usr/bin/env python3
"""Run a small, isolated smoke test against the real image entrypoint."""
import json, os, subprocess, sys, tempfile, time, uuid
from pathlib import Path

def main(argv=None):
    image = (argv or sys.argv[1:])[0] if (argv or sys.argv[1:]) else None
    if not image: print('usage: smoke-image.py IMAGE', file=sys.stderr); return 2
    engine = os.environ.get('CONTAINER_ENGINE', 'podman')
    if engine not in ('docker', 'podman'): print('CONTAINER_ENGINE must be docker or podman', file=sys.stderr); return 2
    name = 'openruyi-smoke-' + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix='openruyi-smoke-') as root:
        config = Path(root) / 'config'; data = Path(root) / 'data'; config.mkdir(); data.mkdir()
        (config / 'tracker.toml').write_text('[obs]\napi_url = "http://127.0.0.1:9"\nweb_url = "http://127.0.0.1:9"\nproject = "test"\n[collector]\nobs_interval_seconds = 3600\nnvchecker_interval_seconds = 3600\n[spec]\nrepo = ""\n')
        cmd=[engine,'run','--rm','--name',name,'--network','none','--read-only','--cap-drop=all','--security-opt=no-new-privileges','--user','10001:10001','--tmpfs','/tmp:rw,nosuid,nodev,size=128m,mode=1777','-v',f'{config}:/config:ro','-v',f'{data}:/data:rw','-e','TRACKER_CONFIG=/config/tracker.toml','-e','TRACKER_DB=/data/state/tracker.sqlite3','-e','HOST=0.0.0.0','-e','PORT=8080','-e','API_PORT=18731',image]
        try:
            p=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            deadline=time.monotonic()+60
            ok=False
            while time.monotonic()<deadline:
                if p.poll() is not None: break
                time.sleep(1)
                probe=subprocess.run([engine,'exec',name,'python3','-c','import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/livez",timeout=2)'],capture_output=True)
                if probe.returncode==0: ok=True; break
            if not ok: raise RuntimeError('livez did not become ready')
            uid=subprocess.check_output([engine,'exec',name,'id','-u'],text=True).strip()
            if uid != '10001': raise RuntimeError('container UID is not 10001')
            return 0
        except Exception as e:
            print(f'smoke failed: {e}', file=sys.stderr); return 1
        finally:
            subprocess.run([engine,'rm','-f',name],capture_output=True)
if __name__ == '__main__': raise SystemExit(main())
