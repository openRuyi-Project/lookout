// The theme works without scripting: set a cookie and redirect to the same site.
import type {APIRoute} from 'astro';
import {safeHref} from '../lib/document';

const THEMES = new Set(['auto', 'light', 'dark']);

function safeReturn(from: string | null): string {
  return from?.startsWith('/') && safeHref(from) ? from : '/';
}

export const GET: APIRoute = ({url, cookies, redirect}) => {
  const to = url.searchParams.get('to') || 'auto';
  const back = safeReturn(url.searchParams.get('from'));
  if (!THEMES.has(to)) return redirect(back, 303);
  if (to === 'auto') {
    cookies.delete('theme', {path: '/'});
  } else {
    cookies.set('theme', to, {path: '/', maxAge: 60 * 60 * 24 * 365, sameSite: 'lax'});
  }
  return redirect(back, 303);
};
