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
    result = follow(client, choice['href'])
    assert (page['total'] if choice['selected'] else result['total']) == choice['count']
    return result
