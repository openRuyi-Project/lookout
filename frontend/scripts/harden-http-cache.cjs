// GHSA-ch52-4w7c-c8xp: max-stale must not revive security-zeroed entries.
// Remove this patch once an upstream release passes tests/http-cache.cjs unchanged.
const {createHash} = require('node:crypto');
const {readFileSync, writeFileSync} = require('node:fs');
const path = require.resolve('http-cache-semantics');
const source = readFileSync(path, 'utf8');
const original = 'ede1cc404a492fa348eb9d97a3007a0d72aa717bd22cd86a56bd0824c19729ca';
const anchor = '        this._assertRequestHasHeaders(req);\n\n        // In all circumstances';
const replacement = '        this._assertRequestHasHeaders(req);\n'
  + '        // Lookout security patch: never reuse security-zeroed responses.\n'
  + '        if (!this.storable() || this.maxAge() === 0) {\n'
  + '            return this._evaluateRequestMissResult(req);\n'
  + '        }\n\n        // In all circumstances';
const pristine = source.includes(replacement) ? source.replace(replacement, anchor) : source;
if (createHash('sha256').update(pristine).digest('hex') !== original || !pristine.includes(anchor)) {
  throw new Error('http-cache-semantics changed; review the security patch against its regression tests');
}
if (source === pristine) writeFileSync(path, source.replace(anchor, replacement));
