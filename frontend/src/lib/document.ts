// This UI boundary imports only display primitives, never monitor payloads.
export type {Text, Cell, Table, Field, Entry, Section, Navigation, Facet,
  Controls, ListingDocument, DetailDocument, DocumentTheme} from './api.generated';

export function safeHref(value?: string | null): string | undefined {
  if (!value || /[\u0000-\u0020\\]/.test(value)) return undefined;
  if (value.startsWith('/') && !value.startsWith('//')) return value;
  try {
    const url = new URL(value);
    if (['https:', 'http:'].includes(url.protocol) && !url.username && !url.password) return url.href;
  } catch { /* Invalid provenance stays visible as text, never executable markup. */ }
  return undefined;
}
