"""Collection status for the same snapshot as the displayed results."""
from tracker.presentation.labels import caption
from tracker.presentation.model import Column, Row, Section, Table
from tracker.presentation.navigation import Links
from tracker.presentation.values import CHECK_LABELS, cell, field, stamp, text


def data_status(collection, coverage, catalog):
    links = Links()
    rows = []
    for monitor in catalog:
        counts = coverage.get(monitor['id'], {})
        values = [text(f'{count} {CHECK_LABELS.get(status, status.replace("_", " "))}',
            href=links.condition('check:' + monitor['id'], status, monitor=monitor['id'], section='coverage'))
            for status, count in counts.items() if count]
        rows.append(Row(key=monitor['id'], cells=[cell([text(caption(monitor['title']))]),
                                                 cell(values or [text('No observations', tone='muted')])]))
    errors = list(dict.fromkeys(collection.get('errors', [])))
    return Section(id='data-status-content', title='Data status' + (' · Degraded' if errors else ''),
        fields=[field('Scope', text(f"All {collection['packages']} packages")),
                field('BuildService observed', stamp(collection.get('obs_updated_at'))),
                field('Versions observed', stamp(collection.get('upstream_updated_at'))),
                field('Last attempt', stamp(collection.get('last_attempt')))],
        notes=errors, table=Table(label='Collection checks across all packages',
            columns=[Column(title='Monitor'), Column(title='Checks')], rows=rows))
