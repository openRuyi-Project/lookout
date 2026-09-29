from urllib.parse import parse_qs, urlsplit

def parsed(href):
    return parse_qs(urlsplit(href).query)


def document(client, params):
    response = client.get('/api/ui/packages', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def follow(client, href):
    response = client.get('/api/ui/packages?' + urlsplit(href).query)
    assert response.status_code == 200, response.text
    return response.json()


def modes(page):
    return [choice for nav in page['controls']['navigation'] for choice in nav['choices']]


def inline_navigation(page):
    return page['controls']['choice_rows']


def facet_destination(client, page, choice):
    """Nonzero choices toggle; zero choices restart with one filter."""
    result = follow(client, choice['href'])
    if choice['count'] == 0:
        query = parsed(choice['href'])
        filters = query.keys() - {'q', 'page', 'per_page'}
        assert len(filters) == 1
        key, = filters
        assert key in {'view', 'maintenance', 'signal', 'build', 'buildsystem'}
        assert len(query[key]) == 1
        assert query.get('q', ['']) == [page['controls']['query']]
        assert query['page'] == ['1']
    else:
        assert (page['total'] if choice['selected'] else result['total']) == choice['count']
    return result
