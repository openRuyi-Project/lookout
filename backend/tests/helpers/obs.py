class FakeOBS:
    def __init__(self,config,names=('binutils',),failed=(),new_hash='h-binutils'):
        self.config=config;self.names=names;self.failed=failed;self.new_hash=new_hash
    def get(self,path):
        if any(key in path for key in self.failed):raise TimeoutError()
        if path.endswith('/_meta'):
            return ('<project name="openruyi">'+''.join(f'<repository name="{t["repository"]}"><arch>{t["architecture"]}</arch></repository>' for t in self.config['targets'])+'</project>').encode()
        if path.endswith('/_result'):
            return ('<resultlist>'+''.join(f'<result project="openruyi" repository="{t["repository"]}" arch="{t["architecture"]}">'+''.join(f'<status package="{n}" code="succeeded"/>' for n in self.names)+'</result>' for t in self.config['targets'])+'</resultlist>').encode()
        if path.endswith('/openruyi'):
            return ('<directory>'+''.join(f'<entry name="{n}"/>' for n in self.names)+'</directory>').encode()
        if path.startswith('/source/openruyi?'):
            return ('<sourceinfolist>'+''.join(f'<sourceinfo package="{n}" srcmd5="new-{n}" rev="2"/>' for n in self.names)+'</sourceinfolist>').encode()
        name=path.split('/')[-1].split('?')[0]
        return f'<sourceinfo package="{name}" srcmd5="{self.new_hash}" rev="2"><version>3.11.0</version></sourceinfo>'.encode()
