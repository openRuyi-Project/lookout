"""Extract script elements for SSR assertions; do not sanitize HTML."""
import json
import sys
from html.parser import HTMLParser


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.current = {'attributes': dict(attrs), 'text': ''}
            self.elements.append(self.current)

    def handle_data(self, data):
        if self.current is not None:
            self.current['text'] += data

    def handle_endtag(self, tag):
        if tag == 'script':
            self.current = None


parser = Scripts()
parser.feed(sys.stdin.read())
parser.close()
print(json.dumps(parser.elements))
