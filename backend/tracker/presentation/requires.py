"""Requires for reading documents; no collection or persistence."""
import json
from urllib.parse import quote

from tracker.presentation.model import Column, Entry, Row, Section, Table
from tracker.presentation.values import cell, field, retained_marker, text, version_value


def requirement_status(requirement, *, target=False):
    status = requirement['target_satisfaction'] if target else requirement['satisfaction']
    reason = requirement.get('target_reason') if target else requirement.get('reason')
    mark, tone = {'satisfied': ('✓', 'positive'), 'unsatisfied': ('✗', 'negative'),
                  'unknown': ('?', 'muted')}[status]
    title = {'satisfied': 'Current source version satisfies the upstream declaration',
             'unsatisfied': 'Current source version does not satisfy the upstream declaration',
             'unknown': 'Cannot determine: ' + (reason or 'unknown').replace('_', ' ')}[status]
    return text(mark, tone=tone, title=title)


def requirement_constraint(requirement, side):
    constraint = requirement.get(side)
    if not constraint:
        return text('Not observed', tone='muted')
    expression = constraint['expression'] or 'any version'
    if requirement['scheme'] == 'numeric_minimum':
        expression = '≥ ' + expression
    return text(expression, kind='code', href=constraint['url'])


def requirement_values(requirement, *, compact=False):
    package = requirement['package']
    label = requirement['name']
    if requirement.get('extras'):
        label += '[' + ', '.join(requirement['extras']) + ']'
    mapped = requirement.get('mapping') in (None, 'mapped')
    name = text(label, href='/packages/' + quote(package, safe='') if package and mapped else None)
    values = [name, requirement_constraint(requirement, 'current'), requirement_status(requirement)]
    if requirement['changed']:
        values += [text('→'), requirement_constraint(requirement, 'target'), requirement_status(requirement, target=True)]
    elif requirement['current'] is None and requirement['target'] is not None:
        values = [name, text('Upgrade:'), requirement_constraint(requirement, 'target'), requirement_status(requirement, target=True)]
    mapping = requirement.get('mapping')
    if mapping in ('not_mapped', 'ambiguous', 'not_packaged'):
        values = [value for value in values if value.text != '?']
        values.append(text({'not_mapped': 'Not mapped', 'ambiguous': 'Ambiguous mapping',
                            'not_packaged': 'Not packaged'}[mapping], tone='muted'))
    if compact and not requirement['changed']:
        values = [value for value in values if value.text != 'any version']
    return values


def requirement_groups(requirements):
    """Fold only identical assessments under different applicability clauses.

    Conditions remain separate evidence, never evaluated or rewritten here.
    Identity, source URLs, extras, scope and assessment are all part of the key.
    """
    grouped = {}
    for requirement in requirements:
        key = (bool(requirement.get('condition')),
               json.dumps({key: value for key, value in requirement.items() if key != 'condition'}, sort_keys=True))
        group = grouped.setdefault(key, (requirement, []))
        condition = requirement.get('condition')
        if condition and condition not in group[1]:
            group[1].append(condition)
    return list(grouped.values())


def requires_sections(result, links):
    requirements = result['data']['requirements']
    if not requirements:
        return []
    sections, conditions = [], []
    for optional, title, suffix in [(False, 'Requires', ''), (True, 'Optional dependencies', '-optional')]:
        groups = requirement_groups([item for item in requirements if (item.get('optional') is True) == optional])
        rows = []
        for number, (requirement, clauses) in enumerate(groups):
            values = requirement_values(requirement)
            observed = requirement['observed'] or {}
            rows.append(Row(key=str(number) + ':' + requirement['dependency'], cells=[
                cell([values[0]]), cell(values[1:]), cell([text(observed.get('version'), kind='code')])]))
            if clauses:
                condition_fields = [field('Applies when (any)', *[text(clause, kind='code') for clause in clauses])]
                if requirement.get('optional') is None:
                    condition_fields.append(field('Optionality', text('Not observed', tone='muted')))
                conditions.append(Entry(heading=values, fields=condition_fields))
        if rows:
            sections.append(Section(id=result['id'] + suffix, title=title, table=Table(
                label='Upstream runtime requirements compared with current source versions',
                columns=[Column(title='Dependency'), Column(title='Required'), Column(title='Current source')], rows=rows)))
    if conditions:
        sections.append(Section(id=result['id'] + '-conditions', title='Dependency conditions',
                                collapsible=True, entries=conditions))
    return sections


def requires_cells(pkg, result, links):
    data = result['data']
    lines = []
    has_optional = any(item.get('optional') is True for item in data['requirements'])
    for optional, label in [(False, 'Runtime'), (True, 'Optional')]:
        groups = requirement_groups([item for item in data['requirements'] if (item.get('optional') is True) == optional])
        if not groups:
            continue
        if has_optional:
            lines.append([text(label, tone='muted')])
        lines.extend(requirement_values(item, compact=True) for item, _ in groups)
    if result.get('dimensions', {}).get('retained:' + result['id']):
        lines.insert(0, [retained_marker(links.to(monitor=result['id'], freshness='retained', check='', section='results'))])
    if any(requirement['changed'] or requirement['current'] is None for requirement in data['requirements']):
        lines.insert(0, version_value(pkg))
    return [cell(*lines)]
