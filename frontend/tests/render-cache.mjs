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

for (const validator of ['"fixture"', 'W/"fixture"', '"other", W/"fixture"', '*']) {
  const response = cache.get('tiny19', validator);
  assert.equal(response.status, 304);
  assert.equal(response.body, null);
  assert.equal(response.headers.get('ETag'), '"fixture"');
}
assert.equal(cache.get('tiny19', '"different"').status, 200);
assert.equal(cache.get('missing', '*'), undefined);

const concurrent = new RenderCache(4096, 2);
const gate = Promise.withResolvers();
let renders = 0;
const produce = async () => {
  renders++;
  await gate.promise;
  concurrent.set('same', html(), body);
  return new Response(body);
};
const requests = [concurrent.resolve('same', produce),
  concurrent.resolve('same', produce, '"fixture"'), concurrent.resolve('same', produce)];
assert.equal(renders, 1);
gate.resolve();
const responses = await Promise.all(requests);
assert.equal(renders, 1);
assert.deepEqual(await Promise.all(responses.map(response => response.text())), ['rendered', '', 'rendered']);
assert.equal(responses[1].status, 304);
assert.equal(concurrent.pending.size, 0);
assert.equal((await concurrent.resolve('same', produce, '"fixture"')).status, 304);
assert.equal(renders, 1);

const failureGate = Promise.withResolvers();
const failed = concurrent.resolve('failure', async () => {
  await failureGate.promise;
  throw new Error('render failed');
});
const recovered = concurrent.resolve('failure', async () => new Response('retry'));
const rejection = assert.rejects(failed, /render failed/);
failureGate.resolve();
await rejection;
assert.equal(await (await recovered).text(), 'retry');
assert.equal(concurrent.pending.size, 0);

// A non-cacheable response belongs only to its originating request.
const cookieGate = Promise.withResolvers();
let cookieCalls = 0;
const cookieResponse = async () => {
  const id = ++cookieCalls;
  await cookieGate.promise;
  const response = new Response('private', {headers: {'Set-Cookie': `id=${id}`}});
  concurrent.set('cookie', response, body);
  return response;
};
const cookies = [concurrent.resolve('cookie', cookieResponse), concurrent.resolve('cookie', cookieResponse)];
cookieGate.resolve();
assert.deepEqual((await Promise.all(cookies)).map(r => r.headers.get('Set-Cookie')), ['id=1', 'id=2']);
assert.equal(concurrent.get('cookie'), undefined);

const saturation = Promise.withResolvers();
const active = Array.from({length: 8}, (_, i) => concurrent.resolve('key' + i, async () => {
  await saturation.promise;
  return new Response('uncached');
}));
assert.equal(concurrent.pending.size, 2);
saturation.resolve();
await Promise.all(active);
assert.equal(concurrent.pending.size, 0);
console.log('PASS render coordination: conditional hits, independent bodies, one producer, failure recovery, cookies and bounded pending keys');
