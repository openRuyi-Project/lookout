export const views = [['all', 'All'], ['updates', 'Updates'], ['untracked', 'Untracked']] as const;

/** Normalize browser input once; the API remains responsible for filtering. */
export function listingQuery(input: URLSearchParams): URLSearchParams {
  const requested = input.get('view') || 'all';
  const view = [...views.map(([key]) => key), 'problems', 'attention'].includes(requested)
    ? requested : 'all';
  const requestedPage = Number(input.get('page') || '1');
  const page = Number.isSafeInteger(requestedPage) && requestedPage > 0
    ? Math.min(requestedPage, 1000000) : 1;
  const query = new URLSearchParams({
    q: (input.get('q') || '').trim().slice(0, 100),
    view,
    buildsystem: (input.get('buildsystem') || '').slice(0, 100),
    maintenance: (input.get('maintenance') || '').slice(0, 40),
    monitor: (input.get('monitor') || '').slice(0, 64),
    check: input.get('monitor') ? (input.get('check') || '').slice(0, 40) : '',
    page: String(page),
    per_page: '100',
  });
  for (const build of input.getAll('build').filter(value => !value.endsWith(':')).slice(0, 16)) {
    query.append('build', build);
  }
  return query;
}

/** Preserve the complete selection when following labels, tabs or pagination. */
export function listingURL(filters: URLSearchParams, changes: Record<string, string> = {}): string {
  const query = new URLSearchParams(filters);
  query.delete('per_page');
  query.set('page', '1');
  for (const [key, value] of Object.entries(changes)) {
    if (value) query.set(key, value);
    else query.delete(key);
  }
  return '/?' + query;
}
