// The theme works without scripting: set a cookie and redirect to the same site.
import type {APIRoute} from 'astro';

const THEMES = new Set(['auto', 'light', 'dark']);

// Only same-origin absolute paths; reject protocol-relative and external targets.
function safeReturn(from: string | null): string {
  if (!from || from[0] !== '/' || from[1] === '/' || from.startsWith('/\\')) return '/';
  return from;
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
