"""Display vocabulary, independent of stored facts and query identities."""
import tomllib
from collections import Counter
from pathlib import Path

_CONFIG = tomllib.loads(Path(__file__).with_suffix('.toml').read_text())
CATALOG = _CONFIG['labels']
STYLES = _CONFIG['styles']
_ICON_USES = Counter(item['icon'] for item in CATALOG.values() if item.get('icon'))


def caption(value):
    return CATALOG.get(value, {}).get('text', value)


def appearance(value):
    return 'label:' + value if 'style' in CATALOG.get(value, {}) else None


def priority(value):
    key = (value or '').removeprefix('label:')
    style = CATALOG.get(key, {}).get('style')
    return STYLES.get(style, {}).get('order', 3)


def palettes():
    return {'label:' + key: {field: STYLES[item['style']][field]
                            for field in ('background', 'foreground')}
            for key, item in CATALOG.items() if 'style' in item}


def facets():
    return [key for key, item in CATALOG.items() if item.get('facet')]


def icon(value):
    return CATALOG.get(value, {}).get("icon")


def icon_only(value):
    return CATALOG.get(value, {}).get("icon_only", _ICON_USES[icon(value)] == 1)
