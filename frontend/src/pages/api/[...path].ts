import type {APIRoute} from 'astro';
import {backendURL} from '../../lib/api';
export const GET: APIRoute = async ({params, url}) => {
  const path = params.path || '';
  if (!/^(?:v1\/(packages(?:\/[^/]+)?|tracks\/[^/]+|targets|status|export)|v2\/packages(?:\/[^/]+)?)$/.test(path)) {
    return Response.json({detail: 'Not found'}, {status: 404});
  }
  try {
    const response = await fetch(backendURL('/api/' + path + url.search), {signal: AbortSignal.timeout(15000), redirect: 'error'});
    const headers = new Headers({'Content-Type': 'application/json; charset=utf-8'});
    const disposition = response.headers.get('Content-Disposition');
    if (disposition) headers.set('Content-Disposition', disposition);
    return new Response(response.body, {status: response.status, headers});
  } catch { return Response.json({detail: 'Snapshot unavailable'}, {status: 503}); }
};
