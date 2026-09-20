# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
from copy import deepcopy
import pytest
from tracker import discover, discover_sources as sources

C={'name':'widget','homepage':'https://widget.example.org','current':'1.2.3','spec_sha256':'a'*64}

def spec(**metadata):
    return {'native_query': {'spec_sha256':'a'*64,'context':{'resolver':6}},
            'metadata':{'version':'1.2.3','sources':[{'number':0,'url':'https://gitlab.example.org/team/widget/-/archive/1.2.3/widget-1.2.3.tar.gz'}],**metadata}}


def test_saved_native_identity_no_clone_read_or_reparse():
    found=sources.hints(C,spec(go_module='github.com/Team/Module/v2'))
    assert found['go_module']=='github.com/Team/Module/v2'
    assert found['source_repository']=='https://gitlab.example.org/team/widget'
    assert found['archive_component']=='widget'
    entry=sources.go_entry(found['go_module'])
    assert entry['url']=='https://proxy.golang.org/github.com/!team/!module/v2/@latest'
    assert not hasattr(sources,'literals') and not hasattr(sources,'spec_git')

@pytest.mark.parametrize('mutate',[
    lambda s:s.update(error='parse failed'),
    lambda s:s['native_query'].update(error='worker failed'),
    lambda s:s['native_query'].update(spec_sha256='b'*64),
    lambda s:s['native_query']['context'].update(resolver=5),
    lambda s:s['metadata'].update(version='2.0'),
])
def test_unverified_or_legacy_metadata_fails_closed(mutate):
    value=spec();mutate(value);assert sources.hints(C,value)=={'hint_error':'native_identity_unverified'}

@pytest.mark.parametrize('module',['%{unknown}','foo.example/../private','%(cat /etc/token)','foo.example//private'])
def test_unknown_native_module_not_guessed(module):
    assert 'go_module' not in sources.hints(C,spec(go_module=module))


def test_only_source_zero_can_identify_project():
    assert 'source_repository' not in sources.hints(C,spec(sources=[{'number':1,'url':'https://github.com/auxiliary/package/archive/1.tar.gz'}]))


def test_component_separates_shared_homepage():
    row={**C,'shared_homepage':True,'homepage':'https://suite.example.org'}
    row.update(sources.hints(C,spec()))
    response={'total_items':2,'items':[
        {'id':1,'name':'widget','homepage':row['homepage'],'versions':['1.2.3'],'stable_versions':['1.3.0']},
        {'id':2,'name':'sibling','homepage':row['homepage'],'versions':['1.2.3'],'stable_versions':['9.0.0']}]}
    assert discover.match(row,response)['project_id']==1
    del row['archive_component'];assert discover.match(row,response)['reason']


def test_source_repository_corroborates_homepage_not_name():
    row={**C,'source_repository':'https://gitlab.example.org/team/widget'}
    response={'total_items':1,'items':[{'id':1,'name':'widget','homepage':'https://gitlab.example.org/team/widget','versions':['1.2.3'],'stable_versions':['1.3.0']}]}
    assert discover.match(row,response)['reason'] is None
    row['source_repository']='https://github.com/another/widget';assert discover.match(row,response)['reason']


def test_shared_forge_root_still_cannot_select_compatibility_line():
    row={**C,'homepage':'https://github.com/team/widget','source_repository':'https://github.com/team/widget','shared_homepage':True,'archive_component':'widget'}
    response={'total_items':1,'items':[{'id':1,'name':'widget','homepage':row['homepage'],'versions':['1.2.3'],'stable_versions':['3.0.0']}]}
    assert discover.match(row,response)['reason']

@pytest.mark.parametrize('v',['b10448','B.02.20','1.9.17p2'])
def test_native_usable_alphabetic_versions_not_blocked(v):assert discover.version(v)==v

@pytest.mark.parametrize('url',['file:///etc/passwd','https://token@github.com/a/b/archive/1','https://github.com:81/a/b/archive/1','https://github.com/a/../archive/1'])
def test_unsafe_repository_urls(url):assert sources.repository(url) is None


def test_go_formal_release_not_pseudo_or_prerelease():
    import jq
    program=jq.compile(sources.go_entry('example.org/module')['filter'])
    assert program.input_value({'Version':'v2.3.4'}).all()==['v2.3.4']
    for v in ['v0.0.0-20260201000000-abcdef','v2.3.4-rc1','v2.3.4-beta.2']:
        assert program.input_value({'Version':v}).all()==[]


def test_null_github_version_url_does_not_abort_batch():
    row={**C,'source_repository':'https://github.com/team/widget'}
    response={'total_items':1,'items':[{'id':1,'backend':'GitHub','version_url':None,'name':'widget','homepage':row['homepage'],'versions':['1.2.3'],'stable_versions':['2.0.0']}]}
    assert discover.match(row,response)['project_id']==1


def test_shared_release_reuse_requires_unanimous_rule(monkeypatch):
    native={'base':{'source':'jq','url':'https://provider.example/base'}}
    config={'native':native,'packages':{},'spec':{}}
    snapshot={'sources':{'base':{'version':'1.2.3'}},'specs':{'base':{'metadata':{'version':'1.2.3','url':'https://suite.example'},'native_query':{'spec_sha256':'x'}}}}
    monkeypatch.setattr(sources,'hints',lambda row,spec:{'source_url':'https://download.example/releases/1.2.3/modules/base-1.2.3.tar.xz','archive_component':'base'})
    row={'name':'component','current':'1.2.3','homepage':'https://suite.example','shared_homepage':True,'source_url':'https://download.example/releases/1.2.3/modules/component-1.2.3.tar.xz','archive_component':'component','reason':'shared'}
    discover.shared_release_entries(config,snapshot,[row]);assert row['reuse_track']=='base'
    native['second']={'source':'jq','url':'https://provider.example/other'};snapshot['sources']['second']=deepcopy(snapshot['sources']['base']);snapshot['specs']['second']=deepcopy(snapshot['specs']['base'])
    row.pop('reuse_track');row.pop('entry');discover.shared_release_entries(config,snapshot,[row]);assert 'reuse_track' not in row
