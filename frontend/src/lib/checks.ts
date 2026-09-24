// Presentation names only. Status ownership and counts remain in the API.
const labels: Record<string, string> = {
  ok: 'Checked', partial: 'Partial evidence', error: 'Check failed', pending: 'Not yet checked',
  not_configured: 'Not configured', not_applicable: 'Not applicable',
  unsupported: 'Metadata unavailable', input_unavailable: 'Input unavailable',
  expired: 'Out of date', input_changed: 'Input changed', schema_changed: 'Format changed',
};

export const checkLabel = (status: string) => labels[status] ?? status.replaceAll('_', ' ');
