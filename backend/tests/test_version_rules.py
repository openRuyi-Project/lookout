import json
import pytest
from tracker import version_rules as rules


def test_group_and_exception_expand():
    text=r'''schema=1
[group.qt6]
packages=["qt3d", "qtwayland"]
package="qt6-{name}"
source="git"
git="https://code.qt.io/qt/{name}.git"
prefix="v"
include_regex='^v6\.[0-9]+\.[0-9]+$'
[package.special]
source="cpan"
cpan="Text-Tabs+Wrap"
'''
    native,origins,bindings,_=rules.expand(text)
    assert native['qt6-qt3d']['git']=='https://code.qt.io/qt/qt3d.git'
    assert origins['qt6-qt3d']==['group','qt6','packages','qt3d']
    assert len(native)==3 and not bindings


@pytest.mark.parametrize('tail',[
    '[package.foo]\nsource="pypi"\npypi="foo"',
    '[group.b]\npackages=["foo"]\nsource="pypi"\npypi="foo"'])
def test_overlapping_owners_rejected(tail):
    with pytest.raises(ValueError,match='duplicate'):
        rules.expand('schema=1\n[group.a]\npackages=["foo"]\nsource="pypi"\npypi="foo"\n'+tail)


def test_missing_or_unused_parameters_rejected():
    with pytest.raises(ValueError):rules.expand('schema=1\n[group.a]\npackages=["foo"]\nsource="git"\ngit="https://example/{repo}"')
    with pytest.raises(ValueError):rules.expand('schema=1\n[group.a]\nsource="pypi"\npypi="{name}"\n[group.a.packages]\nfoo={unused="x"}')


def test_render_roundtrip_no_selection_changes():
    native={f'pkg-{i}':{'source':'git','git':f'https://example.org/qt/qt{i}.git','prefix':'v','include_regex':r'^v6\.[0-9]+\.[0-9]+$'} for i in range(10)}
    text=rules.render(native,{'http_timeout':20},{'pkg-0':{'watch':['pkg-1']}})
    assert rules.expand(text)==(native,rules.expand(text)[1],{'pkg-0':{'watch':['pkg-1']}},{'http_timeout':20})
    assert 'https://example.org/qt/{git}.git' in text
    assert text==rules.render(native,{'http_timeout':20},{'pkg-0':{'watch':['pkg-1']}})


def test_legacy_native_still_reads():
    assert rules.expand('[foo]\nsource="pypi"\npypi="foo"')[0]=={'foo':{'source':'pypi','pypi':'foo'}}


def test_policy_only_bindings():
    with pytest.raises(ValueError):rules.expand('schema=1\n[binding.foo]\nmonitors={eol={product="foo"}}')


def test_named_exception_replaces_whole_rule_and_delete_restores_group(tmp_path):
    p=tmp_path/'groups.toml'
    p.write_text('schema=1\n[group.shared]\nsource="git"\ngit="https://example/{name}"\nprefix="v"\npackages=["foo","bar"]\n')
    original=rules.load(p).entries;before=rules.digest(p)
    special=tmp_path/'foo.toml';special.write_text('source="pypi"\npypi="foo-upstream"\n')
    loaded = rules.load(p)
    native = loaded.entries
    origins = {n: list(o.table) for n, o in loaded.origins.items()}
    locations = {n: o.file for n, o in loaded.origins.items()}
    overridden = sorted(n for n, o in loaded.origins.items() if o.overrides_group)
    assert native['foo']=={'source':'pypi','pypi':'foo-upstream'}
    assert native['bar']==original['bar'] and origins['foo']==[]
    assert locations['foo']==str(special) and overridden==['foo']
    assert rules.digest(p)!=before
    special.unlink()
    assert rules.load(p).entries==original and rules.digest(p)==before


def test_exception_only_package_and_invalid_exception_fail_closed(tmp_path):
    p=tmp_path/'groups.toml';p.write_text('schema=1\n')
    f=tmp_path/'foo.toml';f.write_text('source="pypi"\npypi="foo"\n')
    assert rules.load(p).entries=={'foo':{'source':'pypi','pypi':'foo'}}
    f.write_text('pypi="foo"\n')
    with pytest.raises(ValueError,match='requires a native source'):rules.load(p)


def test_directory_render_retains_overridden_group_fallback(tmp_path):
    native={n:{'source':'pypi','pypi':n} for n in ['foo','bar','baz']}
    override={'foo':{'source':'git','git':'https://example/foo'}}
    for name,text in rules.layout(native,exceptions=override).items():(tmp_path/name).write_text(text)
    assert rules.load(tmp_path/'groups.toml').entries=={**native,**override}
    (tmp_path/'foo.toml').unlink()
    assert rules.load(tmp_path/'groups.toml').entries==native


def test_name_prefix_is_data_not_repeated_member_parameters():
    native={f'python-{name}':{'source':'pypi','pypi':name} for name in ('alpha','beta','gamma','delta')}
    text=rules.render(native)
    assert 'package = "python-{name}"' in text
    assert 'packages = [' in text and '.packages]' not in text
    assert rules.expand(text)[0]==native


def automatic_fixture(name='python-widget', upstream='widget'):
    return {'sources':{name:{'version':'1.0'}},'specs':{name:{'metadata':{'name':name,'version':'1.0','sources':[{'number':0,'url':f'https://registry.example/{upstream}/1.0.tar.gz'}]},'native_query':{'spec_sha256':'a'*64,'context':{'resolver':7}}}}}


def automatic_config():
    return r'''schema=1
[group.python]
match_prefix="python-"
match_source='https://registry.example/(?P<upstream>[a-z]+)/(?P<version>[0-9.]+)\.tar\.gz'
package="python-{name}"
source="pypi"
pypi="{upstream}"
packages=["listed"]
'''


def test_explicit_file_then_member_then_corroborated_heuristic(tmp_path):
    p=tmp_path/'groups.toml';p.write_text(automatic_config())
    explicit=rules.load(p).entries
    assert explicit['python-listed']=={'source':'pypi','pypi':'listed'}
    snapshot=automatic_fixture()
    assert rules.automatic(p.read_text(),snapshot,explicit)[0]['python-widget']=={'source':'pypi','pypi':'widget'}
    (tmp_path/'python-widget.toml').write_text('source="pypi"\npypi="override"\n')
    explicit=rules.load(p).entries
    assert rules.automatic(p.read_text(),snapshot,explicit)[0]=={}
    assert explicit['python-widget']['pypi']=='override'
    # Listed ownership wins even if heuristic metadata names a different upstream.
    assert rules.automatic(p.read_text(),automatic_fixture('python-listed','different'),explicit)[0]=={}


def test_prefix_without_identity_and_packaging_mismatch_remain_untracked():
    text=automatic_config();s=automatic_fixture()
    s['specs']['python-widget']['metadata']['name']='python-different'
    assert rules.automatic(text,s,{})[0]=={}
    assert rules.automatic(text,{'sources':s['sources']},{})[0]=={}
    s=automatic_fixture();s['specs']['python-widget']['native_query']['spec_sha256']='unverified'
    assert rules.automatic(text,s,{})[0]=={}


def test_heuristic_conflict_fails_and_explicit_members_remain_offline():
    text=automatic_config();text+=text.split('schema=1',1)[1].replace('[group.python]','[group.second]')
    with pytest.raises(ValueError,match='ambiguous'):
        rules.automatic(text,automatic_fixture(),{})
    assert rules.expand(automatic_config())[0]['python-listed']['pypi']=='listed'


def test_promotion_requires_snapshot_for_automatic_rules(tmp_path, monkeypatch):
    from tracker import config_change
    config = {'automatic_filters': {'python': {'match_prefix': 'python-'}}}
    monkeypatch.setattr(config_change, 'inputs', lambda path, snapshot: (path, path, config))
    with pytest.raises(ValueError, match='saved snapshot'):
        config_change.plan(tmp_path/'base', tmp_path/'candidate', tmp_path/'runtime', tmp_path/'review')
    assert not (tmp_path/'review').exists()
