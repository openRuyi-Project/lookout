import type {APIRoute} from 'astro';
import {api} from '../lib/api';
import type {DocumentTheme} from '../lib/document';

// Operator-owned distribution data, rendered as a same-origin stylesheet.
// Keep strict CSP: no inline styles, arbitrary CSS, or frontend category palette.
export const GET: APIRoute = async () => {
  const response = await api<DocumentTheme>('/api/ui/theme');
  if (!response.data) return new Response('/* Presentation unavailable; neutral labels remain usable. */',
    {status: 503, headers: {'Content-Type': 'text/css; charset=utf-8'}});
  const rules = Object.entries(response.data.appearances || {}).flatMap(([value, colors]) => {
    if (!/^#[0-9a-f]{6}$/i.test(colors.background) || !/^#[0-9a-f]{6}$/i.test(colors.foreground)) return [];
    const selector = JSON.stringify(encodeURIComponent(value));
    return [`.value-tag[data-appearance=${selector}]{background-color:${colors.background};color:${colors.foreground}}`];
  });
  return new Response(rules.join('\n'), {headers: {'Content-Type': 'text/css; charset=utf-8'}});
};
