/** Process-local HTML only. Every lookup follows a fresh document request. */
export class RenderCache {
  constructor(maxBytes = 8 * 1024 * 1024, maxEntries = 64) {
    this.maxBytes = maxBytes;
    this.maxEntries = maxEntries;
    this.entries = new Map();
    this.bytes = 0;
  }

  /** @param {string} key */
  get(key) {
    const entry = this.entries.get(key);
    if (!entry) return;
    this.entries.delete(key);
    this.entries.set(key, entry);
    return new Response(entry.body, {headers: entry.headers});
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
