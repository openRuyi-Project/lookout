// This UI boundary imports only display primitives, never monitor payloads.
import type {components} from './api.generated';
type Schemas = components['schemas'];
export type Text = Schemas['Text'];
export type Table = Schemas['Table'];
export type Field = Schemas['Field'];
export type Section = Schemas['Section'];
export type Navigation = Schemas['Navigation'];
export type FilterEditor = Schemas['FilterEditor'];
export type Controls = Schemas['Controls'];
export type ListingDocument = Schemas['ListingDocument'];
export type DetailDocument = Schemas['DetailDocument'];
export type DocumentTheme = Schemas['DocumentTheme'];

export function safeHref(value?: string | null): string | undefined {
  if (!value || /[\u0000-\u0020\\]/.test(value)) return undefined;
  if (/^#[A-Za-z][A-Za-z0-9_-]*$/.test(value)) return value;
  if (value.startsWith('/') && !value.startsWith('//')) return value;
  try {
    const url = new URL(value);
    if (['https:', 'http:'].includes(url.protocol) && !url.username && !url.password) return url.href;
  } catch { /* Invalid provenance stays visible as text, never executable markup. */ }
  return undefined;
}
