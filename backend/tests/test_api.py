import json
from fastapi.testclient import TestClient
from tracker import state
from tracker.api import create_app

def client(tmp_path,snapshot):
    db=tmp_path/'test.sqlite3';state.commit(db,snapshot)
    return TestClient(create_app(db)),db

def test_list_filters_counts_pagination(tmp_path,snapshot):
    c,_=client(tmp_path,snapshot)
    d=c.get('/api/v1/packages').json()
    assert d['total']==5 and d['counts']=={'all':5,'updates':2,'problems':1,'attention':2,'untracked':2}
    assert [r['name'] for r in d['items']]==['binutils','foo3','foo4','unknown','untracked']
    assert c.get('/api/v1/packages?q=BIN&view=updates').json()['items'][0]['name']=='binutils'
    assert c.get('/api/v1/packages?q=no-match').json()['total']==0
    assert c.get('/api/v1/packages?per_page=2&page=2').json()['items'][0]['name']=='foo4'
    assert c.get('/api/v1/packages?per_page=2&page=999').json()['page']==3

def test_get_has_no_external_effect(tmp_path,snapshot,monkeypatch):
    c,db=client(tmp_path,snapshot)
    import subprocess,httpx
    def forbidden(*a,**k):raise AssertionError('GET attempted collection')
    monkeypatch.setattr(subprocess,'run',forbidden)
    monkeypatch.setattr(httpx.Client,'request',forbidden)
    before=state.read(db)
    # ASGI TestClient does not invoke network transport; request method replaced so use lower-level send.
    from starlette.testclient import TestClient as TC
    from fastapi import Request
    for path in ['/api/v1/packages','/api/v1/status','/api/v1/targets','/api/v1/tracks/widget@3']:
        request=httpx.Request('GET','http://testserver'+path)
        assert c.send(request).status_code==200
    assert state.read(db)==before

def test_unknown_vs_absent_and_read_only(tmp_path,snapshot):
    c,_=client(tmp_path,snapshot)
    assert c.get('/api/v1/packages/nope').status_code==404
    assert c.get('/api/v1/packages/unknown').json()['relation']=='unknown'
    assert c.get('/api/v1/tracks/missing').status_code==404
    assert c.post('/api/v1/packages').status_code==405
    for query in ['page=0','per_page=201','view=bogus','q='+'x'*101]:
        assert c.get('/api/v1/packages?'+query).status_code==422

def test_schema_export_and_no_secret_config(tmp_path,snapshot):
    c,_=client(tmp_path,snapshot)
    assert '/api/v1/packages' in c.get('/openapi.json').json()['paths']
    export=c.get('/api/v1/export')
    assert len(export.json()['packages'])==5
    assert 'attachment;' in export.headers['content-disposition']
    assert c.get('/docs').status_code==404 # no external Swagger CDN

def test_no_snapshot_is_not_empty_success(tmp_path):
    c=TestClient(create_app(tmp_path/'absent.db'))
    assert c.get('/healthz').status_code==200
    assert c.get('/readyz').status_code==503
    assert c.get('/api/v1/packages').status_code==503

def test_snapshot_reload_same_generation(tmp_path,snapshot):
    c,db=client(tmp_path,snapshot)
    assert c.get('/api/v1/packages').json()['total']==5
    snapshot['sources'].pop('untracked');state.commit(db,snapshot)
    assert c.get('/api/v1/packages').json()['total']==4
