from tests.helpers.config import setup_config
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
from pathlib import Path
import json
import subprocess
import sys
import tomllib
import pytest
from tracker import config as cfg
from tracker import state
from tracker import package
from tracker import config_change

ROOT=Path(__file__).resolve().parents[3]


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


def test_explain_explicit_package_locations_offline_without_state_write(tmp_path):
    path = setup_config(tmp_path / 'config')
    native = path.parent / 'native.toml'
    native.write_text(config_change.edit_tables(native.read_text(), {
        'history': {'source': 'jq', 'url': 'https://release-monitoring.org/api/v2/versions/?project_id=7306',
                    'filter': 'first(.stable_versions[])'},
        'revision': {'source': 'git', 'git': 'https://example.org/fixture.git'},
    }))
    policies = path.parent / 'packages.toml'
    policies.write_text(config_change.edit_tables(policies.read_text(), {'revision': {'comparable': False}}))
    db=tmp_path/'absent.db'
    for name in ['history', 'widget', 'revision']:
        value=package.explain(path,name,db,path)
        assert value['read_only'] and value['rules'][0]['line']>0 and value['rules'][0]['runtime_matches']
        assert value['runtime_binding_matches']
    assert package.explain(path,'history')['rules'][0]['source']['project_id']==7306
    assert package.explain(path,'revision')['binding']['comparable'] is False
    assert not db.exists()


def test_check_calls_native_only_never_collector(tmp_path,monkeypatch):
    from tracker.monitors.version import nvchecker as nv
    path = setup_config(tmp_path / 'config')
    config_dir = path.parent
    def contents():
        return {str(p.relative_to(config_dir)): p.read_bytes()
                for p in config_dir.rglob('*') if p.is_file()}
    before = contents()
    def run(config, previous, now, tracks):
        assert previous == {} and tracks == ['widget']
        return {'widget': {'version': '2.0', 'error': None}}, None
    monkeypatch.setattr(nv, 'run', run)
    database = tmp_path / 'missing.db'
    value = package.check(path, 'widget', database)
    assert value['passed'] and value['state_writes'] is False
    assert not database.exists()
    assert contents() == before


@pytest.mark.parametrize('mutation', ['edit', 'delete'])
def test_check_rejects_rule_directory_mutation(tmp_path, monkeypatch, mutation):
    from tracker.monitors.version import nvchecker as nv
    tracker = setup_config(tmp_path / 'config')
    root = tracker.parent
    def run(*args, **kwargs):
        path = root / 'native.toml'
        if mutation == 'edit':
            path.write_text(path.read_text() + '\n# unexpected write\n')
        else:
            path.unlink()
        return {'widget': {'version': '2.0', 'error': None}}, None
    monkeypatch.setattr(nv, 'run', run)
    with pytest.raises(ValueError, match='configuration changed or unavailable'):
        package.check(tracker, 'widget', tmp_path / 'missing.db')
    assert not (tmp_path / 'missing.db').exists()




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


@pytest.mark.parametrize('change', ['add', 'modify', 'delete'])
def test_native_policy_roundtrip_preserves_monitors_and_operator_settings(tmp_path, change):
    paths = [setup_config(tmp_path / name, operator=(name == 'runtime'))
             for name in ('base', 'candidate', 'runtime')]
    monitor = {'eol': {'product': 'widget'}}
    for path in paths:
        policy = {'monitors': monitor}
        if change != 'add':
            policy['track_label'] = 'old-line'
        policies = path.parent / 'packages.toml'
        policies.write_text(config_change.edit_tables(policies.read_text(), {'widget': policy}))
    policy = {'monitors': monitor}
    if change != 'delete':
        policy['track_label'] = 'new-line'
    candidate = paths[1]
    policies = candidate.parent / 'packages.toml'
    policies.write_text(config_change.edit_tables(policies.read_text(), {'widget': policy}))
    # Only configured paths may affect reviewed policy.
    for path in paths:
        (path.parent / 'unrelated.toml').write_text('[widget]\ntrack_label="unconfigured"\n')
    review = tmp_path / 'review'
    config_change.plan(*paths, review)
    config_change.apply(review, paths[2], tmp_path / 'prepared')
    actual = cfg.load(tmp_path / 'prepared/tracker.toml')
    assert actual['packages']['widget'] == policy
    assert actual['native_options']['http_timeout'] == 30
    assert actual['collector']['obs_interval_seconds'] == 90


def test_multiple_native_changes_report_affected_tracks_and_conflicts(tmp_path):
    paths = [setup_config(tmp_path / name) for name in ('base', 'candidate', 'runtime')]
    for path in paths:
        native = path.parent / 'native.toml'
        native.write_text(config_change.edit_tables(native.read_text(), {
            name: {'source': 'pypi', 'pypi': name} for name in ('a', 'b', 'c')
        }))
    native = paths[1].parent / 'native.toml'
    native.write_text(config_change.edit_tables(native.read_text(), {
        name: {'source': 'pypi', 'pypi': name, 'use_pre_release': True} for name in ('a', 'b', 'c')
    }))
    assert config_change.plan(*paths, tmp_path / 'review')['changed_tracks'] == ['a', 'b', 'c']
    native = paths[2].parent / 'native.toml'
    native.write_text(config_change.edit_tables(native.read_text(), {'b': {'source': 'pypi', 'pypi': 'operator'}}))
    with pytest.raises(ValueError, match='operator changes conflict: b'):
        config_change.plan(*paths, tmp_path / 'rejected')
