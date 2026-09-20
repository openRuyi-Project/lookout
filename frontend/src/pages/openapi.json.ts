import type {APIRoute} from 'astro';
import {api} from '../lib/api';
export const GET: APIRoute = async () => {
  const result = await api('/openapi.json');
  return Response.json(result.data || {detail: 'Schema unavailable'}, {status: result.status});
};
