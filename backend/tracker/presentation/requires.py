"""Requires for reading documents; no collection or persistence."""
import json
from urllib.parse import quote

from tracker.monitors.requires.model import unsatisfied
from tracker.presentation.model import Column, Entry, Row, Section, Table
from tracker.presentation.labels import caption
from tracker.presentation.values import cell, field, retained_marker, text, version_value


DEPENDENCY_TITLES = {'runtime': 'RuntimeDeps', 'build': 'BuildDeps'}


def requirement_status(requirement, *, target=False):
    status = requirement['target_satisfaction'] if target else requirement['satisfaction']
    reason = requirement.get('target_reason') if target else requirement.get('reason')
    mark, tone = {'satisfied': ('✓', 'positive'), 'unsatisfied': ('✗', 'negative'),
                  'unknown': ('?', 'muted'), 'not_applicable': ('—', 'muted')}[status]
    title = {'satisfied': 'Observed dependency version satisfies the upstream declaration',
             'unsatisfied': 'Observed dependency version does not satisfy the upstream declaration',
             'unknown': 'Cannot determine: ' + (reason or 'unknown').replace('_', ' '),
             'not_applicable': 'Dependency condition does not apply to the configured target'}[status]
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
        values.append(text(caption({'not_mapped': 'Not mapped', 'ambiguous': 'Ambiguous mapping',
                            'not_packaged': 'Not packaged'}[mapping]), tone='muted'))
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
    for title, suffix, groups in dependency_groups(requirements):
        rows = []
        for number, (requirement, clauses) in enumerate(groups):
            values = requirement_values(requirement)
            observed = requirement['observed'] or {}
            rows.append(Row(key=str(number) + ':' + requirement['dependency'], cells=[
                cell([values[0]]), cell(values[1:]), cell([text(observed.get('version'), kind='code',
                    href=observed.get('evidence_url'), title=observed.get('origin'))])]))
            if clauses:
                condition_fields = [field('Applies when (any)', *[text(clause, kind='code') for clause in clauses])]
                if requirement.get('optional') is None:
                    condition_fields.append(field('Optionality', text('Not observed', tone='muted')))
                conditions.append(Entry(heading=values, fields=condition_fields))
        if rows:
            sections.append(Section(id=result['id'] + suffix if sections else result['id'], title=title, table=Table(
                label=title + ' compared with current source versions',
                columns=[Column(title='Dependency'), Column(title='Required'), Column(title='Current source')], rows=rows)))
    if conditions:
        sections.append(Section(id=result['id'] + '-conditions', title='Dependency conditions',
                                collapsible=True, entries=conditions))
    return sections


def dependency_groups(requirements):
    for kind, title in DEPENDENCY_TITLES.items():
        for optional in (False, True):
            groups = requirement_groups([item for item in requirements
                                         if item['kind'] == kind and (item.get('optional') is True) == optional])
            if groups:
                suffix = ('-build' if kind == 'build' else '') + ('-optional' if optional else '')
                yield ('Optional ' if optional else '') + title, suffix, groups


def requires_cells(pkg, result, links):
    data = result['data']
    lines = []
    for title, _, groups in dependency_groups(data['requirements']):
        lines.append([text(title, tone='muted')])
        lines.extend(requirement_values(item, compact=True) for item, _ in groups)
    if result.get('dimensions', {}).get('retained:' + result['id']):
        lines.insert(0, [retained_marker(links.to(monitor=result['id'], freshness='retained', check='', section='results'))])
    if any(requirement['changed'] or requirement['current'] is None for requirement in data['requirements']):
        lines.insert(0, version_value(pkg))
    return [cell(*lines)]


def requires_preview(pkg, result, links):
    """Expand changes when requested; otherwise show only resolved conflicts."""
    changes = links.query.get('signal') == result['id']
    visible = [item for item in result['data']['requirements']
               if unsatisfied(item) or changes and item['changed']]
    groups = [group for _, _, members in dependency_groups(visible) for group in members]
    lines = [[text(DEPENDENCY_TITLES[item['kind']] + ':', tone='muted'),
              *requirement_values(item, compact=True),
              *([text('Optional', tone='muted')] if item.get('optional') is True else [])]
             for item, _ in groups]
    return lines
