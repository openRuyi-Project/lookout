"""Concise fixture queries, serialized through the same public JSON contract."""
from urllib.parse import parse_qsl, urlencode, urlsplit

from tracker.readmodel.query import Condition, FilterQuery, Group


def conjunction(dimensions):
    terms = tuple(Condition(dimension=dimension, value=value)
                  for dimension, values in dimensions.items()
                  for value in ([values] if isinstance(values, str) else values) if value)
    return FilterQuery(groups=(Group(conditions=terms),)) if terms else FilterQuery()


def query_url(path, dimensions=None, **parameters):
    if dimensions:
        parameters['filters'] = conjunction(dimensions).encode()
    return path + ('?' + urlencode(parameters, doseq=True) if parameters else '')


def terms_in(href):
    """Flatten only for assertions about membership, never to evaluate logic."""
    query = filter_in(href)
    return [(term.dimension, term.value) for group in query.groups for term in group.conditions]


def filter_in(href):
    query, _ = FilterQuery.extract(parse_qsl(urlsplit(href).query, keep_blank_values=True))
    return query
