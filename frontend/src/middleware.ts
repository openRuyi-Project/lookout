import {defineMiddleware} from 'astro:middleware';
import {createHash} from 'node:crypto';
export const onRequest = defineMiddleware(async (context, next) => {
  const response = await next();
  const scripts = context.url.pathname === '/' ? "'self'" : "'none'";
  response.headers.set('Content-Security-Policy', `default-src 'self'; script-src ${scripts}; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`);
  response.headers.set('X-Content-Type-Options', 'nosniff');
  response.headers.set('Referrer-Policy', 'no-referrer');
  response.headers.set('X-Frame-Options', 'DENY');
  response.headers.set('Cache-Control', 'no-store');
  // Revalidate actual rendered HTML, including query and theme, before reuse.
  // Never cache errors, redirects, cookie-setting responses or raw observations.
  if (context.request.method === 'GET' && response.status === 200
      && (response.headers.get('Content-Type')?.startsWith('text/html')
          || context.url.pathname === '/presentation.css')
      && !response.headers.has('Set-Cookie')) {
    const body = await response.arrayBuffer();
    const etag = `W/"${createHash('sha256').update(new Uint8Array(body)).digest('hex')}"`;
    const headers = new Headers(response.headers);
    headers.set('Cache-Control', 'private, no-cache');
    headers.set('ETag', etag);
    headers.append('Vary', 'Cookie');
    const validators = context.request.headers.get('If-None-Match')?.split(',') || [];
    const unchanged = validators.some(value => value.trim() === '*'
      || value.trim().replace(/^W\//, '') === etag.slice(2));
    if (unchanged) {
      headers.delete('Content-Length');
      return new Response(null, {status: 304, headers});
    }
    return new Response(body, {status: response.status, headers});
  }
  return response;
});
