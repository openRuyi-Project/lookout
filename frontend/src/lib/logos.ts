import catalog from '../assets/logos/catalog.json';

const files = import.meta.glob<string>('../assets/logos/*.svg', {
  eager: true, query: '?url&no-inline', import: 'default',
});

export function logo(id?: string | null) {
  if (!id || !Object.hasOwn(catalog, id)) return null;
  const entry = catalog[id as keyof typeof catalog];
  const src = files[`../assets/logos/${id}.svg`];
  return src ? {src, monochrome: entry.monochrome} : null;
}
