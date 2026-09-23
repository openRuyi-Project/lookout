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
