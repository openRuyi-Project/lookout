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
