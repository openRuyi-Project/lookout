"""Keep the last completed evidence separate from the next check's state."""


def last_result(observation):
    if not observation:
        return None
    if observation.get('last_result'):
        return observation['last_result']
    if observation.get('checked_at') or observation.get('findings'):
        return observation
    return None


def retain(proposed, previous):
    saved = last_result(previous)
    return {**proposed, 'last_result': saved} if saved else proposed


def visible(observation):
    """Borrow matching evidence, never relabel a different query as current.

    The outer record owns the attempted query/status; the saved record retains
    its own fingerprint, interpretation version and observation timestamps.
    """
    saved = observation.get('last_result')
    if not saved or not observation.get('query_fingerprint'):
        return observation
    if saved.get('query_fingerprint') != observation['query_fingerprint']:
        return observation
    if checks := observation.get('scope_checks'):
        saved_checks = saved.get('scope_checks', {})
        missing = {scope for scope, check in checks.items()
                   if scope in saved_checks and not check.get('checked_at')}
        if not missing:
            return observation
        checks = dict(checks)
        for scope in missing:
            attempt = checks[scope]
            checks[scope] = {**saved_checks[scope], **attempt,
                             'checked_at': saved_checks[scope].get('checked_at')}
        # A failed scope has no new evidence. Keep its failure state and dated
        # result, without replacing a sibling scope's successful empty response.
        findings = [f for f in observation.get('findings', []) if f['scope'] not in missing]
        findings.extend(f for f in saved.get('findings', []) if f['scope'] in missing)
        return {**observation, 'findings': findings, 'scope_checks': checks,
                'checked_at': observation.get('checked_at') or saved.get('checked_at')}
    fields = ('findings', 'scope_checks', 'checked_at', 'evidence_revision', 'changed_at')
    return {**observation, **{key: saved[key] for key in fields if key in saved}, '_retained': True}
