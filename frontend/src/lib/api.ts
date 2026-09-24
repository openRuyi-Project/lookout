export function backendURL(path: string): URL {
  // Host is operator configuration, never a query parameter or request Host header.
  const base = process.env.TRACKER_API_URL || 'http://127.0.0.1:18731';
  return new URL(path, base);
}
export async function api<T>(path: string, timeoutMs = 8000): Promise<{status: number; data: T | null}> {
  try {
    const response = await fetch(backendURL(path), {signal: AbortSignal.timeout(timeoutMs), redirect: 'error'});
    return {status: response.status, data: response.ok ? await response.json() as T : null};
  } catch { return {status: 503, data: null}; }
}
