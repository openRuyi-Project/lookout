"""A bounded, non-executable reading contract. No provider types or HTML.

The website owns layout and interaction; read presenters own the meaning of
values. This is a disposable projection, never a second stored observation.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic import Field as Constraint

from tracker.readmodel.query import MAX_QUERY_NODES, FilterQuery


class DocumentModel(BaseModel):
    model_config = ConfigDict(extra='forbid', json_schema_serialization_defaults_required=True)


class Text(DocumentModel):
    text: str
    href: str | None = None
    title: str | None = None
    kind: Literal['text', 'code', 'tag', 'time'] = 'text'
    variant: Literal['outline', 'solid'] = 'outline'
    tone: Literal['normal', 'muted', 'positive', 'negative', 'notice'] = 'normal'
    appearance: str | None = None
    decoration: Literal['dashed'] | None = None
    datetime: str | None = None


class Cell(DocumentModel):
    lines: list[list[Text]] = []


class Choice(DocumentModel):
    appearance: str | None = None
    icon: str | None = None
    label: str
    href: str | None
    selected: bool = False
    count: int | None = None


class Navigation(DocumentModel):
    label: str
    show_label: bool = True
    icon: str | None = None
    choices: list[Choice]


class Column(DocumentModel):
    title: str
    role: Literal['identity', 'value', 'status'] = 'value'


class RowNote(DocumentModel):
    column: int = Constraint(ge=0)
    span: int = Constraint(ge=1)
    values: list[Text]


class Row(DocumentModel):
    key: str
    id: str | None = Constraint(default=None, pattern=r'^[A-Za-z][A-Za-z0-9_-]*$')
    cells: list[Cell]
    notes: list[RowNote] = []


class Table(DocumentModel):
    label: str
    columns: list[Column]
    rows: list[Row]
    empty: str = 'No matching packages.'

    @model_validator(mode='after')
    def rectangular(self):
        if any(len(row.cells) != len(self.columns) for row in self.rows):
            raise ValueError('Every row must have one cell per column')
        if any(note.column + note.span > len(self.columns) for row in self.rows for note in row.notes):
            raise ValueError('Row notes must stay within the table columns')
        return self


class Field(DocumentModel):
    label: str
    href: str | None = None
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


class Parameter(DocumentModel):
    name: str
    value: str


class FilterCondition(Choice):
    logic: str


class FilterGroup(DocumentModel):
    id: int
    active: bool
    select: str
    logic: str
    conditions: list[FilterCondition]
    clear: str
    add: str | None


class FilterEditor(DocumentModel):
    query: FilterQuery
    active_group: int
    node_limit: int = MAX_QUERY_NODES
    groups: list[FilterGroup]
    operators: list[Choice]
    clear: str


class Controls(DocumentModel):
    editor: FilterEditor
    action: str = '/'
    query: str = ''
    hidden: list[Parameter] = []
    active: list[Choice] = []
    navigation: list[Navigation] = []
    choice_rows: list[Navigation] = []


class ListingDocument(DocumentModel):
    schema_version: Literal[1] = 1
    title: str
    navigation: Navigation
    global_navigation: list[Navigation] = []
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
    notices: list[str] = []


class Palette(DocumentModel):
    background: str
    foreground: str


class DocumentTheme(DocumentModel):
    appearances: dict[str, Palette] = {}
