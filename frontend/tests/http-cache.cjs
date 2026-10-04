const assert = require('node:assert/strict');
const {createRequire} = require('node:module');
const requireInstalled = createRequire(process.cwd() + '/package.json');
const Policy = requireInstalled('http-cache-semantics');
const request = {url: 'https://fixture.invalid/resource', method: 'GET', headers: {host: 'fixture.invalid'}};
for (const headers of [
  {'set-cookie': 'session=private'},
  {'cache-control': 'private, max-age=3600'},
  {'cache-control': 'no-store'},
  {'cache-control': 'no-cache'},
  {'cache-control': 'max-age=0'},
]) {
  const policy = new Policy(request, {status: 200, headers}, {shared: true});
  for (const maxStale of ['max-stale', 'max-stale=31536000']) {
    const candidate = {...request, headers: {...request.headers, 'cache-control': maxStale}};
    assert.equal(policy.satisfiesWithoutRevalidation(candidate), false, JSON.stringify(headers));
    assert.equal(policy.evaluateRequest(candidate).response, undefined);
    assert.equal(policy.evaluateRequest(candidate).revalidation.synchronous, true);
  }
}
const publicPolicy = new Policy(request, {status: 200, headers: {'cache-control': 'public, max-age=60'}});
const storedAt = publicPolicy.now();
publicPolicy.now = () => storedAt + 70_000;
assert.equal(publicPolicy.satisfiesWithoutRevalidation(request), false);
assert.equal(publicPolicy.satisfiesWithoutRevalidation({...request,
  headers: {...request.headers, 'cache-control': 'max-stale=20'}}), true);
console.log('PASS HTTP cache policy: private/no-store/no-cache/cookies cannot be revived; public stale reuse preserved');
