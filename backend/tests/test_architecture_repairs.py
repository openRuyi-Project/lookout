# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
from pathlib import Path
import json
import subprocess
import sys
import tomllib
import pytest
from tracker import config as cfg, state, package, config_change

ROOT=Path(__file__).resolve().parents[2]


def test_public_anitya_identity_survives_without_query_secrets():
    value=cfg.public_source({'source':'jq','url':'https://user:password@release-monitoring.org/api/v2/versions/?project_id=7306&token=SECRET'})
    assert value['project_id']==7306 and value['project_url']=='https://release-monitoring.org/project/7306/'
    assert all(s not in json.dumps(value) for s in ['SECRET','password','token=','user:'])
    for url in ['https://other.example/api/v2/versions/?project_id=7306','https://release-monitoring.org/api/v2/versions/?project_id=7&project_id=8']:
        assert 'project_id' not in cfg.public_source({'source':'jq','url':url})


def test_source_link_uses_writer_provenance_not_frontend_default():
    assert cfg.spec_source_url({'url':'https://github.com/other/packages.git'},'pkg','feature/fix')=='https://github.com/other/packages/tree/feature%2Ffix/SPECS/pkg'
    assert cfg.spec_source_url({'url':'https://gitlab.example.org/team/specs.git'},'pkg','abc')=='https://gitlab.example.org/team/specs/-/tree/abc/SPECS/pkg'
    assert cfg.spec_source_url({'url':'https://other.example/specs.git'},'pkg','main') is None
    template={'url':'https://other.example/specs.git','source_url_template':'https://other.example/src/{ref}/{path}'}
    assert cfg.spec_source_url(template,'a b','main').endswith('/main/SPECS/a%20b')
    for bad in ['javascript:{ref}/{path}','https://password@forge/{ref}/{path}','https://forge/{secret}/{path}']:
        assert cfg.spec_source_url({**template,'source_url_template':bad},'p','r') is None


def test_generated_api_contract_is_exact():
    result=subprocess.run([sys.executable,str(ROOT/'scripts/api-types.py'),'--check'],text=True,capture_output=True)
    assert result.returncode==0,result.stdout+result.stderr


def test_explain_real_package_locations_offline_without_state_write(tmp_path):
    db=tmp_path/'absent.db'
    for name in ['ModemManager','agg','go-github-campoy-embedmd-embedmd']:
        value=package.explain(ROOT/'config/tracker.toml',name,db,ROOT/'config/tracker.toml')
        assert value['read_only'] and value['rules'][0]['line']>0 and value['rules'][0]['runtime_matches']
        assert value['runtime_binding_matches']
    assert package.explain(ROOT/'config/tracker.toml','ModemManager')['rules'][0]['source']['project_id']==7306
    assert package.explain(ROOT/'config/tracker.toml','go-github-campoy-embedmd-embedmd')['binding']['comparable'] is False
    assert not db.exists()


def test_check_calls_native_only_never_collector(tmp_path,monkeypatch):
    from tracker import nv
    import shutil
    config_dir = tmp_path / 'config'
    shutil.copytree(ROOT / 'config', config_dir)
    def contents():
        return {str(p.relative_to(config_dir)): p.read_bytes()
                for p in config_dir.rglob('*') if p.is_file()}
    before = contents()
    def run(config, previous, now, tracks):
        assert previous == {} and tracks == ['ModemManager']
        return {'ModemManager': {'version': '1.24.2', 'error': None}}, None
    monkeypatch.setattr(nv, 'run', run)
    database = tmp_path / 'missing.db'
    value = package.check(config_dir / 'tracker.toml', 'ModemManager', database)
    assert value['passed'] and value['state_writes'] is False
    assert not database.exists()
    assert contents() == before


@pytest.mark.parametrize('mutation', ['edit'])
def test_check_rejects_rule_directory_mutation(tmp_path, monkeypatch, mutation):
    import shutil
    from tracker import nv
    root = tmp_path / 'config'
    shutil.copytree(ROOT / 'config', root)
    def run(*args, **kwargs):
        path = root / 'versions/nvchecker.toml'
        if mutation == 'edit':
            path.write_text(path.read_text() + '\n# unexpected write\n')
        elif mutation == 'create':
            (root / 'versions/unexpected-native.toml').write_text('source="pypi"\npypi="unexpected"\n')
        else:
            path.unlink()
        return {'ModemManager': {'version': '1.24.2', 'error': None}}, None
    monkeypatch.setattr(nv, 'run', run)
    with pytest.raises(ValueError, match='native configuration changed'):
        package.check(root / 'tracker.toml', 'ModemManager', tmp_path / 'missing.db')
    assert not (tmp_path / 'missing.db').exists()


def setup_config(path,version='base',operator=False):
    path.mkdir()
    (path/'tracker.toml').write_text('[obs]\napi_url="https://obs.example"\nweb_url="https://obs.example"\nproject="scope"\n'+''.join(f'[[targets]]\nid="{n}"\nlabel="{n}"\nrepository="{n}"\narchitecture="{n}"\n' for n in 'abc')+'[collector]\nnvchecker_config="native.toml"\nobs_interval_seconds='+('90' if operator else '60')+'\n')
    (path/'native.toml').write_text('# retained operator comment\n[__config__]\nhttp_timeout='+('30' if operator else '20')+'\n[widget]\nsource="pypi"\npypi='+json.dumps(version)+'\n')
    return path/'tracker.toml'


def test_plan_apply_rebase_keeps_operator_settings_and_detects_drift(tmp_path):
    base=setup_config(tmp_path/'base');candidate=setup_config(tmp_path/'candidate','changed');runtime=setup_config(tmp_path/'runtime',operator=True)
    (runtime.parent/'keys.toml').write_text('operator-secret');(runtime.parent/'keys.toml').chmod(0o600)
    before={p.name:p.read_bytes() for p in runtime.parent.iterdir()};review=tmp_path/'review'
    plan=config_change.plan(base,candidate,runtime,review)
    assert plan['changed_tracks']==['widget'] and plan['changed_bindings']==[]
    assert tomllib.loads((review/'native.toml').read_text())['__config__']['http_timeout']==30
    assert '# retained operator comment' in (review/'native.toml').read_text()
    assert 'operator-secret' not in json.dumps(plan) and not (review/'keys.toml').exists()
    out=tmp_path/'prepared';result=config_change.apply(review,runtime,out)
    assert result['activation_required'] and result['runtime_unchanged']
    assert cfg.load(out/'tracker.toml')['collector']['obs_interval_seconds']==90
    assert cfg.load(out/'tracker.toml')['native']['widget']['pypi']=='changed'
    assert (out/'keys.toml').read_text()=='operator-secret' and (out/'keys.toml').stat().st_mode&0o777==0o600
    assert before=={p.name:p.read_bytes() for p in runtime.parent.iterdir()}
    runtime.write_text(runtime.read_text()+'\n# operator changed\n')
    with pytest.raises(ValueError,match='drift'):config_change.apply(review,runtime,tmp_path/'rejected')
    assert not (tmp_path/'rejected').exists()


def test_promotion_conflict_and_tampered_review_rejected(tmp_path):
    base=setup_config(tmp_path/'base');candidate=setup_config(tmp_path/'candidate','changed');runtime=setup_config(tmp_path/'runtime','operator')
    with pytest.raises(ValueError,match='conflict'):config_change.plan(base,candidate,runtime,tmp_path/'conflict')
    runtime.parent.joinpath('native.toml').write_text(base.parent.joinpath('native.toml').read_text())
    config_change.plan(base,candidate,runtime,tmp_path/'review')
    f=tmp_path/'review/native.toml';f.write_text(f.read_text()+'\n# changed after review\n')
    with pytest.raises(ValueError,match='reviewed candidate changed'):config_change.apply(tmp_path/'review',runtime,tmp_path/'no')


def test_phase_ownership_is_enforced(snapshot):
    with pytest.raises(ValueError,match='ownership'):state.merge(snapshot,'upstreams',{'sources':{}})
    with pytest.raises(ValueError,match='ownership'):state.merge(snapshot,'specs',{}, {'nvchecker':{}})
    result=state.merge(snapshot,'specs',{'specs':{'widget':{'head':'abc'}}})
    assert result['sources']==snapshot['sources'] and result['tracks']==snapshot['tracks']
    assert result['generation']==snapshot['generation']+1
    result['sources']['binutils']['version']='changed'
    assert snapshot['sources']['binutils']['version']!='changed'


def test_central_migration_preserves_operator_options_secrets_and_single_owner(tmp_path):
    from tracker import version_rules
    base=setup_config(tmp_path/'base');candidate=setup_config(tmp_path/'candidate');runtime=setup_config(tmp_path/'runtime',operator=True)
    original=cfg.load(candidate)
    candidate.write_text(candidate.read_text().replace('native.toml','versions.toml'))
    (candidate.parent/'native.toml').unlink()
    (candidate.parent/'versions.toml').write_text(version_rules.render(original['native'],original['native_options'],{'widget':{'track_label':'stable'}}))
    secret=runtime.parent/'keys.toml';secret.write_text('private-fixture');secret.chmod(0o600)
    before={p.name:p.read_bytes() for p in runtime.parent.iterdir()}
    review=tmp_path/'review';plan=config_change.plan(base,candidate,runtime,review)
    assert plan['schema']==2 and plan['changed_tracks']==[]
    config_change.apply(review,runtime,tmp_path/'out')
    result=cfg.load(tmp_path/'out/tracker.toml')
    assert result['native']==original['native']
    assert result['native_options']['http_timeout']==30
    assert result['collector']['obs_interval_seconds']==90
    assert result['version_bindings']['widget']=={'track_label':'stable'}
    assert 'packages' not in tomllib.loads((tmp_path/'out/tracker.toml').read_text()) or not tomllib.loads((tmp_path/'out/tracker.toml').read_text())['packages']
    assert not (tmp_path/'out/native.toml').exists()
    assert (tmp_path/'out/keys.toml').read_bytes()==secret.read_bytes()
    assert before=={p.name:p.read_bytes() for p in runtime.parent.iterdir()}


def test_group_change_reports_each_member_and_rejects_operator_conflict(tmp_path):
    from tracker import version_rules
    paths=[setup_config(tmp_path/n) for n in ('base','candidate','runtime')]
    for path in paths:
        path.write_text(path.read_text().replace('native.toml','versions.toml'))
        (path.parent/'native.toml').unlink()
        (path.parent/'versions.toml').write_text('schema=1\n[group.registry]\nsource="pypi"\npypi="{name}"\nuse_pre_release=false\npackages=["a","b","c"]\n')
    f=paths[1].parent/'versions.toml';f.write_text(f.read_text().replace('false','true'))
    result=config_change.plan(*paths,tmp_path/'review')
    assert result['changed_tracks']==['a','b','c']
    native,_,bindings,options=version_rules.expand((paths[2].parent/'versions.toml').read_text())
    native['b']['pypi']='operator-identity'
    (paths[2].parent/'versions.toml').write_text(version_rules.render(native,options,bindings))
    with pytest.raises(ValueError,match='operator changes conflict: b'):
        config_change.plan(*paths,tmp_path/'conflict')


def test_directory_promotion_exception_precedence_keyfile_and_added_file_drift(tmp_path):
    from tracker import version_rules
    base=setup_config(tmp_path/'base');candidate=setup_config(tmp_path/'candidate');runtime=setup_config(tmp_path/'runtime',operator=True)
    # Keep three group members, then override one without deleting its fallback.
    for p in (base,candidate,runtime):
        text=(p.parent/'native.toml').read_text()
        for name in ['a','b','c']:
            text+='\n['+name+']\nsource="pypi"\npypi="'+name+'"\n'
        (p.parent/'native.toml').write_text(text)
    f=runtime.parent/'native.toml';f.write_text(f.read_text().replace('[__config__]','[__config__]\nkeyfile="keys.toml"'))
    (runtime.parent/'keys.toml').write_text('private fixture')
    original=cfg.load(candidate)
    (candidate.parent/'versions').mkdir()
    for name,text in version_rules.layout(original['native'],original['native_options'],exceptions={'a':{'source':'git','git':'https://example/a'}}).items():
        (candidate.parent/'versions'/name).write_text(text)
    candidate.write_text(candidate.read_text().replace('native.toml','versions/groups.toml'));(candidate.parent/'native.toml').unlink()
    result=config_change.plan(base,candidate,runtime,tmp_path/'review')
    assert result['changed_tracks']==['a'] and result['schema']==3
    config_change.apply(tmp_path/'review',runtime,tmp_path/'out')
    prepared=cfg.load(tmp_path/'out/tracker.toml')
    assert prepared['native']['a']=={'source':'git','git':'https://example/a'}
    assert prepared['group_native']['a']=={'source':'pypi','pypi':'a'}
    assert prepared['native_options']['keyfile']=='../keys.toml'
    assert prepared['native_options']['http_timeout']==30
    assert not (tmp_path/'out/native.toml').exists()
    explanation=package.explain(tmp_path/'out/tracker.toml','a')
    assert explanation['rules'][0]['file'].endswith('/versions/a.toml')
    assert explanation['rules'][0]['overrides_group']
    assert explanation['rules'][0]['group_origin'][:1]==['group']
    (tmp_path/'out/versions/a.toml').unlink()
    assert cfg.load(tmp_path/'out/tracker.toml')['native']['a']=={'source':'pypi','pypi':'a'}
    # A file added after review changes the complete input set, not only one hash.
    config_change.plan(tmp_path/'out/tracker.toml',tmp_path/'out/tracker.toml',tmp_path/'out/tracker.toml',tmp_path/'review2')
    (tmp_path/'out/versions/new.toml').write_text('source="pypi"\npypi="new"\n')
    with pytest.raises(ValueError):config_change.apply(tmp_path/'review2',tmp_path/'out/tracker.toml',tmp_path/'rejected')
