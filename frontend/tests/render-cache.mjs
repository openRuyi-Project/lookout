import assert from 'node:assert/strict';
import {RenderCache} from '../src/lib/render-cache.mjs';

const html = () => new Response(null, {headers: {'Content-Type': 'text/html', ETag: '"fixture"'}});
const body = new TextEncoder().encode('rendered').buffer;
const cache = new RenderCache(512, 2);
cache.set('a', html(), body);
cache.set('b', html(), body);
assert.equal(await cache.get('a').text(), 'rendered');
cache.set('c', html(), body);
assert.equal(cache.get('b'), undefined); // access refreshed LRU order
assert.equal(await cache.get('a').text(), 'rendered'); // reads do not consume cached bodies
cache.get('a').headers.set('ETag', 'changed');
assert.equal(cache.get('a').headers.get('ETag'), '"fixture"');
for (const response of [new Response(null, {status: 503}),
  new Response(null, {status: 302}), new Response(null, {headers: {'Set-Cookie': 'theme=dark'}}),
  new Response(null, {headers: {'Content-Type': 'application/json'}})]) {
  cache.set('unsafe', response, body);
  assert.equal(cache.get('unsafe'), undefined);
}
cache.set('large', html(), new ArrayBuffer(513));
assert.equal(cache.get('large'), undefined);
for (let i = 0; i < 20; i++) cache.set('tiny' + i, html(), new ArrayBuffer(0));
assert.ok(cache.entries.size <= 2);
assert.ok(cache.bytes <= 512);
const bounded = new RenderCache(512, 100);
for (let i = 0; i < 20; i++) bounded.set('page' + i, html(), new ArrayBuffer(100));
assert.ok(bounded.bytes <= 512);
assert.ok(bounded.entries.size < 20);
console.log('PASS render cache: isolated responses, LRU, byte/entry bounds, unsafe responses excluded');
