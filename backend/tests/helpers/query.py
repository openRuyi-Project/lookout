"""Concise fixture queries, serialized through the public ordered parameter contract."""
from urllib.parse import parse_qsl, urlencode, urlsplit

from tracker.readmodel.query import Condition, FilterQuery


def conjunction(dimensions):
    terms = tuple(Condition(dimension=dimension, value=value)
                  for dimension, values in dimensions.items()
                  for value in ([values] if isinstance(values, str) else values) if value)
    return FilterQuery(tail=terms) if terms else FilterQuery()


def query_url(path, dimensions=None, **parameters):
    pairs = list(parameters.items()) + conjunction(dimensions or {}).parameters()
    return path + ('?' + urlencode(pairs, doseq=True) if pairs else '')


def terms_in(href):
    """Flatten only for assertions about membership, never to evaluate logic."""
    query = filter_in(href)
    return [(term.dimension, term.value) for term in (*query.tail, *(item for group in query.groups for item in group.conditions))]


def filter_in(href):
    query, _ = FilterQuery.extract(parse_qsl(urlsplit(href).query, keep_blank_values=True))
    return query
