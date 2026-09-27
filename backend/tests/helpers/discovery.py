def response():
    return {'total_items': 1, 'items': [{'id': 123, 'name': 'widget',
            'homepage': 'http://github.com/Team/Widget/',
            'versions': ['2.0rc1', '1.9.17p2', '1.9.17'],
            'stable_versions': ['1.9.17p2', '1.9.17']}]}


def snapshot():
    return {'sources': {'widget': {'version': '1.9.17p2'}},
            'specs': {'widget': {'metadata': {'name': 'widget', 'version': '1.9.17p2',
                       'url': 'https://github.com/team/widget'},
                       'head': 'fixed-head', 'native_query': {'spec_sha256': 'a' * 64}}}}
