import type {APIRoute} from 'astro';
import {api} from '../lib/api';
export const GET: APIRoute = async () => {
  const result = await api('/readyz');
  return Response.json(result.data || {status: 'unavailable'}, {
    status: result.status,
    headers: {'Cache-Control': 'no-store'},
  });
};
