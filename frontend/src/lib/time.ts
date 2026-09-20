// Observation times are shown in UTC, never inferred from collection attempts.
export function utcTime(value: string | null | undefined): string {
  if (!value) return '—';
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString().slice(0, 19).replace('T', ' ') : '—';
}
