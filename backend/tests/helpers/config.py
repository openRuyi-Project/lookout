import json

def setup_config(path,version='base',operator=False):
    path.mkdir()
    (path/'tracker.toml').write_text('[obs]\napi_url="https://obs.example"\nweb_url="https://obs.example"\nproject="scope"\n'+''.join(f'[[targets]]\nid="{n}"\nlabel="{n}"\nrepository="{n}"\narchitecture="{n}"\n' for n in 'abc')+'[collector]\nnvchecker_config="native.toml"\nobs_interval_seconds='+('90' if operator else '60')+'\n')
    (path/'native.toml').write_text('# retained operator comment\n[__config__]\nhttp_timeout='+('30' if operator else '20')+'\n[widget]\nsource="pypi"\npypi='+json.dumps(version)+'\n')
    return path/'tracker.toml'
