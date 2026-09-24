"""Uses the real nvchecker executable and jq source, with only HTTP input fixture substituted."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import re
import tomllib
from pathlib import Path
import subprocess
import threading
from tracker.nv import import_events
from tracker.state import utcnow

def test_native_jq_branch_selection(tmp_path):
    fixtures=Path(__file__).parent/'fixtures'
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(fixtures)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        config=tmp_path/'nvchecker.toml'
        config.write_text((fixtures/'nvchecker.toml').read_text().replace('FIXTURE_PORT',str(server.server_port)))
        p=subprocess.run(['nvchecker','--logger=json','--json-log-fd=1','-c',str(config)],capture_output=True,text=True,timeout=40)
        print('NVCHECKER STDOUT:',p.stdout,'STDERR:',p.stderr,'EXIT:',p.returncode)
        assert p.returncode==0
        result,error=import_events(p.stdout,{'widget@3':{},'widget@4':{}},{},utcnow())
        assert result['widget@3']['version']=='3.10.0'
        assert result['widget@4']['version']=='4.2.0'
        assert not error
    finally:
        server.shutdown();server.server_close();thread.join()

def test_upstream_rolls_without_rewriting_toml(tmp_path):
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self,*args):pass
    payload=tmp_path/'versions.json'
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(tmp_path)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        path=tmp_path/'native.toml'
        path.write_text('[widget]\nsource="jq"\nurl="http://127.0.0.1:'+str(server.server_port)+'/versions.json"\nfilter=".stable_versions[]"\ninclude_regex="^3[.][0-9]+[.][0-9]+$"\n')
        digest=hashlib.sha256(path.read_bytes()).hexdigest();previous={}
        for version in ('3.10.0','3.11.0'):
            payload.write_text(json.dumps({'stable_versions':[version,'4.99.0']}))
            p=subprocess.run(['nvchecker','--logger=json','--json-log-fd=1','--failures','-c',str(path)],capture_output=True,text=True,timeout=40)
            print('ROLLING NVCHECKER:',p.stdout,'STDERR:',p.stderr,'EXIT:',p.returncode)
            assert p.returncode==0
            previous,error=import_events(p.stdout,{'widget':{}},previous,utcnow())
            assert previous['widget']['version']==version and not error
            assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
    finally:
        server.shutdown();server.server_close();thread.join()

def test_native_sparse_index_excludes_yanked_and_other_streams(tmp_path):
    native=__import__('tracker.version_rules',fromlist=['load']).load(Path(__file__).resolve().parents[2]/'config/versions/nvchecker.toml').entries
    sparse=[v for v in native.values() if v.get('source')=='regex' and v.get('url','').startswith('https://index.crates.io/')]
    assert sparse, 'the actual production index rule is the test input'
    regex=sparse[0]['regex']
    versions=[('0.4.7',False),('0.4.8+metadata',False),('0.4.99',True),
              ('0.4.100-rc.1',False),('0.4.100-alpha.1',False),('0.4.100-beta',False),
              ('0.4.100-dev',False),('0.4.100-rc+meta',False),('0.5.0',False),('10.4.100',False)]
    records=[{'name':'fixture','vers':v,'deps':[],'yanked':y} for v,y in versions]
    payload='\n'.join(json.dumps(r,separators=(',',':')) for r in records)+'\n'
    assert '0.4.99' not in re.findall(regex,payload)
    # Cargo fields may be reordered; whitespace does not change their meaning.
    assert re.findall(regex,'{"yanked": false , "vers": "0.4.8", "features":{"vers":[]}}\n')==['0.4.8']
    assert re.findall(regex,'{"vers":"0.4.8", "yanked":true}\n')==[]
    (tmp_path/'index').write_text(payload)
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(tmp_path)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        entry={'source':'regex','url':f'http://127.0.0.1:{server.server_port}/index',
               'regex':regex,'include_regex':r'^0\.4\.[0-9]+(?:\+[^ ]+)?$',
               'from_pattern':'[+].*$','to_pattern':''}
        path=tmp_path/'native.toml';path.write_text('[fixture]\n'+'\n'.join(k+' = '+json.dumps(v) for k,v in entry.items()))
        p=subprocess.run(['nvchecker','--logger=json','--json-log-fd=1','--failures','-c',str(path)],capture_output=True,text=True,timeout=40)
        print('SPARSE NVCHECKER:',p.stdout,'STDERR:',p.stderr,'EXIT:',p.returncode)
        assert p.returncode==0
        result,error=import_events(p.stdout,{'fixture':entry},{},utcnow())
        assert result['fixture']['version']=='0.4.8' and not error
        # A withdrawn maintenance line is an error, not proof of being current.
        (tmp_path/'index').write_text('{"name":"fixture","vers":"0.4.8","yanked":true}\n')
        p=subprocess.run(['nvchecker','--logger=json','--json-log-fd=1','--failures','-c',str(path)],capture_output=True,text=True,timeout=40)
        print('YANKED NVCHECKER:',p.stdout,'STDERR:',p.stderr,'EXIT:',p.returncode)
        assert p.returncode==3
        retained,_=import_events(p.stdout,{'fixture':entry},result,utcnow())
        assert retained['fixture']['version']=='0.4.8' and retained['fixture']['error']
        assert retained['fixture']['fetched_at']==result['fixture']['fetched_at']
    finally:
        server.shutdown();server.server_close();thread.join()


def test_crates_sources_agree_for_current_release_line_policies():
    """Compare pinned native sources, not a tracker reimplementation of selection."""
    import asyncio
    from nvchecker.api import GetVersionError, RawResult, RichResult
    from nvchecker.core import _process_result
    from nvchecker_source import cratesio, regex
    from tracker import package_identity
    from tracker.version_rules import load

    native = load(Path(__file__).resolve().parents[2] / 'config/versions/nvchecker.toml').entries
    policies = {}
    for entry in native.values():
        if entry.get('source') != 'regex' or not entry.get('url', '').startswith('https://index.crates.io/'):
            continue
        identity = package_identity.from_native(entry)
        candidate = {key: value for key, value in entry.items() if key not in ('source', 'url', 'regex')}
        candidate.update(source='cratesio', cratesio=identity['name'])
        assert package_identity.from_native(candidate) == identity
        policies.setdefault(entry['include_regex'], (entry, candidate))
    assert policies

    class Responses:
        def __init__(self, records):
            self.records = records

        async def get_json(self, url):
            return {'versions': [{'num': version, 'yanked': yanked} for version, yanked in self.records]}

        async def get(self, key, fetch):
            return '\n'.join(json.dumps({'yanked': yanked, 'vers': version})
                             for version, yanked in self.records)

    async def selected(source, entry, records):
        try:
            versions = await source.get_version('fixture', entry, cache=Responses(records))
        except GetVersionError as error:
            versions = error
        result = _process_result(RawResult('fixture', versions, entry))
        return result.version if isinstance(result, RichResult) else None

    async def compare():
        for pattern, (entry, candidate) in policies.items():
            release = pattern.split('(?:', 1)[0].removeprefix('^').replace(r'\.', '.').replace('[0-9]+', '7')
            assert re.fullmatch(pattern, release)
            records = [(release, False), (release + '+build.5', False),
                       (release + '-rc.1', False), (release + '-beta.1', False),
                       (release + '+withdrawn', True), ('9999.0.0', False)]
            for rows, expected in ((records, release), ([(release, True)], None),
                                   ([(release + '-rc.1', False)], None), ([], None)):
                assert await selected(regex, entry, rows) == expected
                assert await selected(cratesio, candidate, rows) == expected

    asyncio.run(compare())


def test_crates_native_run_preserves_empty_selection_evidence(tmp_path, monkeypatch, config):
    """Real run/subset/CLI/import path; only provider HTTP origins use a fixture."""
    import os
    import sys
    import tomlkit
    from tracker import nv, version_rules

    production = version_rules.load(Path(__file__).resolve().parents[2] / 'config/versions/nvchecker.toml').entries
    sample = next(entry for entry in production.values()
                  if entry.get('source') == 'regex' and entry.get('url', '').startswith('https://index.crates.io/'))
    rows = [('0.4.7', False), ('0.4.8+metadata', False), ('0.4.99', True),
            ('0.4.100-rc.1', False), ('0.5.0', False)]

    def write_responses(versions):
        (tmp_path / 'index').write_text('\n'.join(json.dumps({'vers': v, 'yanked': y}) for v, y in versions))
        (tmp_path / 'fixture').write_text(json.dumps({'versions': [{'num': v, 'yanked': y} for v, y in versions]}))

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        root = f'http://127.0.0.1:{server.server_port}'
        binary = tmp_path / 'bin' / 'nvchecker'
        binary.parent.mkdir()
        binary.write_text(f'#!{sys.executable}\n'
                          'from nvchecker_source import cratesio\n'
                          f'cratesio.API_URL = {root + "/%s"!r}\n'
                          'from nvchecker.__main__ import main\nmain()\n')
        binary.chmod(0o755)
        monkeypatch.setenv('PATH', str(binary.parent) + os.pathsep + os.environ['PATH'])
        old = {**sample, 'url': root + '/index', 'include_regex': r'^0\.4\.[0-9]+(?:\+[^ ]+)?$'}
        candidate = {key: value for key, value in old.items() if key not in ('source', 'url', 'regex')}
        candidate.update(source='cratesio', cratesio='fixture')
        config['native'] = {'sparse': old, 'api': candidate}
        path = tmp_path / 'rules.toml'
        # Tornado is a locked runtime dependency; neither fixture needs remote access.
        path.write_text(tomlkit.dumps({'__config__': {'httplib': 'tornado'}, **config['native']}))
        config.update(nvpath=str(path), nv_digest=version_rules.digest(path))
        config['collector']['nvchecker_timeout_seconds'] = 30
        before = path.read_bytes()
        write_responses(rows)
        first, error = nv.run(config, {}, utcnow(), tracks=['sparse', 'api'])
        assert error is None
        assert [first[name]['version'] for name in ('sparse', 'api')] == ['0.4.8', '0.4.8']
        assert not any(value.get('error') for value in first.values())
        write_responses([('0.4.8', True)])
        second, error = nv.run(config, first, utcnow(), tracks=['sparse', 'api'])
        assert error is None  # CLI reports per-track errors through its JSON events.
        for name in first:
            assert second[name]['version'] == first[name]['version']
            assert second[name]['fetched_at'] == first[name]['fetched_at']
            assert second[name]['error'] == 'nvchecker no matching version'
        assert path.read_bytes() == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
