import type {APIRoute} from 'astro';
import {api} from '../lib/api';
export const GET: APIRoute = async () => {
  const result = await api<{status: string}>('/healthz', 2000);
  const ok = result.status === 200 && result.data?.status === 'ok';
  return Response.json({status: ok ? 'ok' : 'unavailable'}, {
    status: ok ? 200 : 503,
    headers: {'Cache-Control': 'no-store'},
  });
};
