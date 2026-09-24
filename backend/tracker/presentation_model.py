"""A bounded, non-executable reading contract. No provider types or HTML.

The website owns layout and interaction; read presenters own the meaning of
values. This is a disposable projection, never a second stored observation.
"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, model_validator


class DocumentModel(BaseModel):
    model_config = ConfigDict(extra='forbid', json_schema_serialization_defaults_required=True)


class Text(DocumentModel):
    text: str
    href: str | None = None
    title: str | None = None
    kind: Literal['text', 'code', 'tag', 'time'] = 'text'
    tone: Literal['normal', 'muted', 'positive', 'negative', 'notice'] = 'normal'
    appearance: str | None = None
    datetime: str | None = None


class Cell(DocumentModel):
    lines: list[list[Text]] = []


class Column(DocumentModel):
    title: str
    role: Literal['identity', 'value', 'status'] = 'value'


class Row(DocumentModel):
    key: str
    cells: list[Cell]


class Table(DocumentModel):
    label: str
    columns: list[Column]
    rows: list[Row]
    empty: str = 'No matching packages.'

    @model_validator(mode='after')
    def rectangular(self):
        if any(len(row.cells) != len(self.columns) for row in self.rows):
            raise ValueError('Every row must have one cell per column')
        return self


class Field(DocumentModel):
    label: str
    values: list[Text]


class Entry(DocumentModel):
    heading: list[Text]
    fields: list[Field] = []


class Section(DocumentModel):
    id: str
    title: str
    collapsible: bool = False
    fields: list[Field] = []
    table: Table | None = None
    entries: list[Entry] = []
    notes: list[str] = []


class Choice(DocumentModel):
    label: str
    href: str
    selected: bool = False
    count: int | None = None


class Navigation(DocumentModel):
    label: str
    choices: list[Choice]


class Option(DocumentModel):
    value: str
    label: str
    count: int | None = None
    selected: bool = False


class Facet(DocumentModel):
    id: str
    name: str
    label: str
    options: list[Option]


class Parameter(DocumentModel):
    name: str
    value: str


class Controls(DocumentModel):
    action: str = '/'
    query: str = ''
    hidden: list[Parameter] = []
    facets: list[Facet] = []
    active: list[Choice] = []
    navigation: list[Navigation] = []


class ListingDocument(DocumentModel):
    schema_version: Literal[1] = 1
    title: str
    navigation: Navigation
    controls: Controls
    table: Table
    total: int
    page: int
    pages: int
    pagination: list[Choice] = []
    meta: list[Field] = []
    notices: list[str] = []


class DetailDocument(DocumentModel):
    schema_version: Literal[1] = 1
    title: str
    subtitle: str | None = None
    identity: list[Text] = []
    links: list[Text] = []
    context: list[Section] = []
    sections: list[Section]


class Palette(DocumentModel):
    background: str
    foreground: str


class DocumentTheme(DocumentModel):
    appearances: dict[str, Palette] = {}
