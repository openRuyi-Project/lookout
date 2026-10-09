/** Return a bodyless validator response without reading or copying the body. */
export function notModified(headers, validator) {
  const etag = headers.get('ETag');
  if (!etag || !validator?.split(',').some(value => value.trim() === '*'
      || value.trim().replace(/^W\//, '') === etag.replace(/^W\//, ''))) return;
  const selected = new Headers(headers);
  selected.delete('Content-Length');
  return new Response(null, {status: 304, headers: selected});
}

/** Process-local HTML only. Every lookup follows a fresh document request. */
export class RenderCache {
  constructor(maxBytes = 8 * 1024 * 1024, maxEntries = 64) {
    this.maxBytes = maxBytes;
    this.maxEntries = maxEntries;
    this.entries = new Map();
    this.bytes = 0;
    this.pending = new Map();
  }

  /** @param {string} key @param {string | null} [validator] */
  get(key, validator) {
    const entry = this.entries.get(key);
    if (!entry) return;
    this.entries.delete(key);
    this.entries.set(key, entry);
    const headers = new Headers(entry.headers);
    return notModified(headers, validator) || new Response(entry.body, {headers});
  }

  /** Coalesce cacheable renders, never share a consumed or cookie-setting response.
   * @param {string} key
   * @param {() => Promise<Response>} produce
   * @param {string | null} [validator]
   */
  async resolve(key, produce, validator) {
    const cached = this.get(key, validator);
    if (cached) return cached;
    const pending = this.pending.get(key);
    if (pending) {
      await pending;
      return this.get(key, validator) || produce();
    }
    // Bound coordination as well as stored HTML; saturation falls back to SSR.
    if (this.pending.size >= this.maxEntries) return produce();
    const result = produce();
    this.pending.set(key, result.then(() => {}, () => {}));
    try { return await result; }
    finally { this.pending.delete(key); }
  }

  /** @param {string} key @param {Response} response @param {ArrayBuffer} body */
  set(key, response, body) {
    if (response.status !== 200 || response.headers.has('Set-Cookie')
        || !response.headers.get('Content-Type')?.startsWith('text/html')) return;
    const headers = [...response.headers];
    const size = body.byteLength + Buffer.byteLength(JSON.stringify(headers)) + Buffer.byteLength(key);
    if (size > this.maxBytes) return;
    const old = this.entries.get(key);
    if (old) this.bytes -= old.size;
    this.entries.delete(key);
    this.entries.set(key, {body, headers, size});
    this.bytes += size;
    while (this.bytes > this.maxBytes || this.entries.size > this.maxEntries) {
      const oldest = this.entries.keys().next().value;
      this.bytes -= this.entries.get(oldest).size;
      this.entries.delete(oldest);
    }
  }
}
