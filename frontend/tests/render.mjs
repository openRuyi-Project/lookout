// Render the production SSR bundle against deterministic API observations.
import assert from 'node:assert/strict';
import {spawn, spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {once} from 'node:events';
import {createServer, request} from 'node:http';
import {gunzipSync, brotliDecompressSync} from 'node:zlib';
import {runInNewContext} from 'node:vm';

const targets = ['rva23', 'rva20', 'x86_64'].map(id => ({id, label: id, repository: id, architecture: 'riscv64'}));
const makePackage = (name, patch = {}) => ({
  buildsystem: null, buildsystem_status: 'not_declared', maintenance: [], maintenance_findings: [], monitor_checks: [], presentation: {buildsystems: {}},
  name, current: '2.0', latest: '2.1', relation: 'outdated', track: name, track_label: name,
  current_build_success: true, last_successful_version: '2.0', stale: false,
  needs_attention: false, upstream_updated_at: '2026-09-19T10:50:00Z', detail_url: `/packages/${name}`, version_error: null,
  builds: targets.map(target => ({target: target.id, label: target.label, repository: target.repository,
    architecture: target.architecture, raw_status: 'succeeded', text: '✓', kind: 'ok', issue: false,
    log_url: null, stale: false, updated_at: '2026-09-19T11:10:00Z', matches_source: true,
    last_success: {version: '2.0', time: '2026-09-19T11:00:00+00:00', srcmd5: 'a'}, flavors: []})),
  spec: {source_path: `SPECS/${name}`, source_url: `https://gitlab.example.org/team/packaging/-/tree/review/SPECS/${name}`, metadata: {summary: 'Fixture package', url: 'https://example.org/upstream'}, changelog: [
    {commit: 'abcdef0123456789', author: 'Fixture author', date: '2026-09-19T09:00:00Z', subject: 'Fixture change', signed_off_by: []},
  ], error: null},
  ...patch,
});
const packages = [
  makePackage('security', {maintenance_findings: [1, 2].map(i => ({
    monitor: 'security', id: `CVE-2026-100${i}`, label: 'Advisory', title: `CVE-2026-100${i}`,
    facts: [{key: 'Observed version', code: 'query', value: '2.0', source: 'OSV', url: 'https://api.osv.dev/v1/query', status: 'observed'},
      {key: 'Exploit probability', code: 'epss_probability', value: 0.00396, source: 'FIRST', url: 'https://api.first.org/data/v1/epss', status: 'observed'},
      {key: 'Reported fixes', code: 'fixed_events', value: ['3.0'], source: 'OSV', url: 'https://osv.dev/vulnerability/fixture', status: 'observed'},
      {key: 'KEV', value: i === 1 ? false : null, source: 'CISA', url: 'https://www.cisa.gov/known-exploited-vulnerabilities-catalog', status: i === 1 ? 'observed' : 'unavailable'}],
    evidence_url: `https://nvd.nist.gov/vuln/detail/CVE-2026-100${i}`,
    scope: 'current', tags: [], stale: false,
  }))}),
  makePackage('license-evidence', {maintenance_findings: [{
    monitor: 'license', id: 'license-target', label: 'LicenseChange', title: 'MIT → Apache-2.0',
    evidence_url: 'https://example.org/license', scope: 'upgrade', target_version: '2.1',
    tags: [], stale: false, facts: [
      {key: 'Licenses', value: ['MIT', 'Apache-2.0'], source: 'Registry', url: 'https://example.org/license', status: 'observed'},
      {key: 'Unavailable field', value: null, source: 'Registry', url: 'https://example.org/license', status: 'unavailable'},
      {key: 'False field', value: false, source: 'Registry', url: 'https://example.org/license', status: 'observed'},
    ],
  }]}),
  makePackage('success', {buildsystem: 'custom', buildsystem_status: 'declared', maintenance: [{label:'NewSignal', count:2, stale:false}]}),
  makePackage('watch-preview', {watch: [
    {id:'widget@preview',version:'2.2rc1',error:null,stale:false},
    {id:'widget@nightly',version:'2.3dev1',error:'fetch failed',stale:false},
    {id:'widget@missing',error:'no result',stale:true},
  ]}),
  makePackage('ahead', {relation: 'ahead'}),
  ...[false, null].map(matches_source => makePackage(matches_source === false ? 'old-source' : 'unknown-source', {
    builds: makePackage('base').builds.map(build => ({...build, matches_source})),
  })),
  makePackage('arch-version', {builds: makePackage('base').builds.map(build => ({...build, last_success: {...build.last_success, version: '2.0.arch'}}))}),
  makePackage('failed-same-version', {builds: makePackage('base').builds.map(build => ({...build, kind: 'error', issue: true, raw_status: 'failed', text: 'Failed', matches_source: false}))}),
  makePackage('long-version', {current: '0.7+git20231216.05e79eb', latest: null, relation: 'untracked', track: null, builds: makePackage('base').builds.map(build => ({...build, last_success: {...build.last_success, version: '0.7+git20231216.05e79eb'}}))}),
  makePackage('failed', {current_build_success: false, last_successful_version: '9.9-shared-should-not-render', stale: true, builds: makePackage('base').builds.map((build, i) => ({...build, text: 'Failed', kind: 'error', issue: true, raw_status: 'failed', stale: true, matches_source: false, last_success: {version: ['1.9', '1.8', '1.7'][i], time: '2026-09-19T11:00:00Z', srcmd5: String(i)}}))}),
  makePackage('untracked', {relation: 'untracked', track: null, current_build_success: false}),
  makePackage('untracked-unavailable-source', {relation: 'unknown', track: null, current: null, current_build_success: null}),
  makePackage('unknown', {relation: 'unknown', current_build_success: null, last_successful_version: null}),
  makePackage('unresolved-version', {builds: [{...makePackage('base').builds[0], last_success: {version: null, time: '2026-09-19T11:00:00Z', srcmd5: 'macro'}}]}),
  makePackage('multibuild', {builds: [{...makePackage('base').builds[0], last_success: null, flavors: [
    {package: 'multibuild:one', text: '✓', kind: 'ok', raw_status: 'succeeded', stale: false, log_url: null, updated_at: '2026-09-19T11:10:00Z', last_success: {version: '1.1', time: '2026-09-19T10:00:00Z', srcmd5: 'one'}},
    {package: 'multibuild:two', text: 'Failed', kind: 'error', issue: true, raw_status: 'failed', stale: false, log_url: null, updated_at: '2026-09-19T11:10:00Z', last_success: {version: '1.0', time: '2026-09-18T10:00:00Z', srcmd5: 'two'}},
  ]}]}),
  makePackage('missing-history', {current_build_success: null, last_successful_version: null,
    builds: targets.map(target => ({target: target.id, label: target.label, repository: target.repository,
      architecture: target.architecture, raw_status: 'unknown', text: 'No result', kind: 'muted',
      log_url: null, stale: false, updated_at: null, matches_source: null, last_success: null, flavors: []}))}),
];
// The fixture keeps concise package variants above; this is its only wire mapping.
// The fixture builds domain inputs; pages receive only reading documents.
const catalog = [
  {id: 'source', title: 'Source', kind: 'source'},
  {id: 'version', title: 'Version', kind: 'version'},
  {id: 'build', title: 'Build', kind: 'build'},
  {id: 'security', title: 'Security', kind: 'evidence'},
  {id: 'license', title: 'License', kind: 'evidence'},
  {id: 'yanked', title: 'Release files', kind: 'evidence'},
];
packages.push(makePackage('yanked', {
  maintenance: [{label: 'Withdrawn', count: 1, stale: false}],
  maintenance_findings: [{monitor: 'yanked', id: 'withdrawn', label: 'Withdrawn', title: 'Release withdrawn',
    scope: 'current', target_version: null, tags: [], stale: false, evidence_url: 'https://example.org/release',
    facts: [{key: 'Files', value: 3, source: 'Registry', url: 'https://example.org/release', status: 'observed'}]}],
}));
function monitored(pkg, detail = true, focus = '') {
  const check = {status: 'ok', stale: false, checked_at: '2026-09-19T11:10:00Z', attempted_at: null,
    error: null, note: null, changed_at: null, evidence_revision: null};
  const data = {
    source: {kind: 'source', version: pkg.current, revision: 'fixture', buildsystem: pkg.buildsystem,
      buildsystem_status: pkg.buildsystem_status, ...pkg.spec, obs: {}},
    version: {kind: 'version', current: pkg.current, latest: pkg.latest, relation: pkg.relation, track: pkg.track,
      track_label: pkg.track_label, stale: pkg.stale, error: pkg.version_error, last_known_relation: pkg.relation,
      updated_at: pkg.upstream_updated_at, upstream: {}, watch: pkg.watch || []},
    build: {kind: 'build', targets: targets.map(t => pkg.builds.find(b => b.target === t.id) || {...makePackage('base').builds.find(b => b.target === t.id), text: 'No result', raw_status: 'unknown', kind: 'muted', matches_source: null, last_success: null}), source_version: pkg.current,
      source_success: pkg.current_build_success, last_successful_version: pkg.last_successful_version},
  };
  const monitors = Object.fromEntries(catalog.map(m => [m.id, {id: m.id, title: m.title, check: {...check},
    data: data[m.id] || {kind: 'evidence', labels: m.id === 'yanked' ? pkg.maintenance : [],
      findings: pkg.maintenance_findings.filter(f => f.monitor === m.id)}}]));
  for (const result of Object.values(monitors)) {
    if (result.data.kind !== 'evidence') continue;
    const findings = result.data.findings;
    const labels = new Map();
    for (const finding of findings) {
      for (const label of new Set([finding.label, ...finding.tags])) {
        const previous = labels.get(label) || {label, count: 0, stale: false};
        labels.set(label, {...previous, count: previous.count + 1, stale: previous.stale || finding.stale});
      }
    }
    if (findings.length) result.data.labels = [...labels.values()];
    result.data.finding_count = findings.length;
    result.data.entries = result.id === focus ? findings.slice(0, 3).map(({id, title, evidence_url, stale}) =>
      ({id, title, evidence_url, stale})) : [];
  }
  monitors.build.check.checked_at = pkg.builds.every(b => b.updated_at) ? pkg.builds[0].updated_at : null;
  if (!detail) {
    for (const result of Object.values(monitors)) {
      for (const key of ['findings', 'metadata', 'changelog', 'obs', 'upstream', 'watch']) delete result.data[key];
    }
  }
  return {name: pkg.name, detail_url: pkg.detail_url, monitors, presentation: pkg.presentation};
}
const fixtureBridge = fileURLToPath(new URL('../../backend/tests/presentation_fixture.py', import.meta.url));
function project(operation, payload, query = {}) {
  const result = spawnSync(process.env.PYTHON || 'python3', [fixtureBridge], {
    input: JSON.stringify({operation, payload, query}), encoding: 'utf8',
  });
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}
let lastQuery = new URLSearchParams();
let unavailable = false;
let health = 'ok';
let ready = {status: 200, body: {status: 'degraded', generation: 1}};
const mock = createServer((req, res) => {
  if (unavailable) { res.writeHead(503, {'Content-Type':'application/json'});res.end('{}');return; }
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/healthz') {
    if (health === 'stall') return;
    if (health === 'disconnected') { req.socket.destroy(); return; }
    res.writeHead(health === 'error' ? 503 : 200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({status: health === 'ok' ? 'ok' : 'not-ok'})); return;
  }
  if (url.pathname === '/readyz') {
    res.writeHead(ready.status, {'Content-Type': 'application/json'});
    res.end(JSON.stringify(ready.body)); return;
  }
  if (url.pathname === '/api/ui/packages' && url.searchParams.get('monitor') === 'not-registered') {
    res.writeHead(422, {'Content-Type': 'application/json'}); res.end('{}'); return;
  }
  if (url.pathname === '/api/ui/packages') lastQuery = url.searchParams;
  const selected = packages.find(pkg => url.pathname === `/api/ui/packages/${pkg.name}`);
  const focus = url.searchParams.get('monitor') || '';
  const section = url.searchParams.get('check') ? 'coverage' : url.searchParams.get('section') || 'results';
  const evidenceFocus = catalog.some(m => m.id === focus && m.kind === 'evidence');
  const resultRows = packages.filter(pkg => pkg.maintenance_findings.some(f => f.monitor === focus));
  const rows = evidenceFocus && section === 'results' ? resultRows : packages;
  const payload = url.pathname === '/api/ui/theme'
    ? {buildsystems: {custom: {background: '#123456', foreground: '#ffffff'}}}
    : selected ? monitored(selected) : {
      monitors: catalog, check_statuses: {ok: packages.length}, section,
      result_count: resultRows.length, coverage_count: packages.length,
      presentation: {buildsystems: {custom: {background: '#123456', foreground: '#ffffff'}}},
      buildsystems: {custom: 1}, maintenance_labels: {NewSignal: 1},
      build_statuses: Object.fromEntries(targets.map(target => [target.id, [{value:'issues',label:'Issues',count:2}, {value:'blocked',label:'Blocked',count:1}]])),
      items: (url.searchParams.get('q') === 'quiet' ? rows.map(pkg=>({...pkg,maintenance:[],maintenance_findings:[]})) : rows).map(pkg => monitored(pkg, true, focus)),
      total: rows.length, page: 1, per_page: 100, pages: 1,
      counts: {all: packages.length, updates: 3, problems: 1, attention: 1, untracked: 1}, targets,
      collection: {obs_updated_at: '2026-09-19T11:10:00Z', upstream_updated_at: '2026-09-19T10:50:00Z', last_attempt: null, mode: 'live', errors: ['intentional fixture error'], generation: 1,
        packages: packages.length, tracked_packages: packages.length - 1}};
  const query = Object.fromEntries(url.searchParams);
  query.build = url.searchParams.getAll('build');
  query.section = section;
  const document = project(url.pathname === '/api/ui/theme' ? 'theme' : selected ? 'detail' : 'list', payload, query);
  res.writeHead(200, {'Content-Type': 'application/json'}); res.end(JSON.stringify(url.pathname.startsWith('/api/ui/') ? document : payload));
});
mock.listen(0, '127.0.0.1'); await once(mock, 'listening');
const reserve = createServer(); reserve.listen(0, '127.0.0.1'); await once(reserve, 'listening');
const port = reserve.address().port; await new Promise(resolve => reserve.close(resolve));
const child = spawn(process.execPath, ['server.mjs'], {env: {...process.env,
  HOST: '127.0.0.1', PORT: String(port), TRACKER_API_URL: `http://127.0.0.1:${mock.address().port}`}, stdio: ['ignore', 'pipe', 'pipe']});
let logs = ''; child.stdout.on('data', chunk => logs += chunk); child.stderr.on('data', chunk => logs += chunk);
async function read(path, cookie) {
  const response = await fetch(`http://127.0.0.1:${port}${path}`, {headers: cookie ? {cookie} : {}});
  assert.equal(response.status, 200, `${path}: ${logs}`); return response.text();
}
function assertCollapsedChecksLast(html) {
  const details = [...html.matchAll(/<details\b([^>]*)>([^]*?)<\/details>/g)];
  assert.equal(details.length, 1, 'only Checks is collapsible');
  const [whole, attributes, body] = details[0];
  assert.doesNotMatch(attributes, /(?:^|\s)open(?:\s|=|$)/, 'Checks starts closed');
  assert.match(body, /^\s*<summary\b[^>]*>Checks<\/summary>/);
  assert.match(body, /Collection checks/);
  assert.match(html.slice(details[0].index + whole.length), /^\s*<\/div>/, 'Checks is the final main section');
  const changelog = html.match(/<section\b[^>]*id="changelog"[^>]*>/);
  if (changelog) assert.ok(changelog.index < details[0].index, 'Checks follows Changelog');
}
async function wire(path, headers = {}, method = 'GET') {
  return new Promise((resolve, reject) => {
    const req = request({host:'127.0.0.1',port,path,method,headers}, response => {
      const chunks=[];response.on('data', chunk=>chunks.push(chunk));response.on('end',()=>resolve({status:response.statusCode,headers:response.headers,body:Buffer.concat(chunks)}));response.on('error',reject);
    });req.on('error',reject);req.end();
  });
}
try {
  for (let retry = 0; retry < 100; retry++) {
    try { await read('/livez'); break; } catch (error) {
      if (retry === 99 || child.exitCode !== null) throw new Error(`SSR did not start: ${logs}`, {cause: error});
      await new Promise(resolve => setTimeout(resolve, 50));
    }
  }
  async function probe(path, expected, body) {
    const result = await fetch(`http://127.0.0.1:${port}${path}`, {signal: AbortSignal.timeout(5000)});
    assert.equal(result.status, expected, path);
    assert.equal(result.headers.get('cache-control'), 'no-store');
    assert.deepEqual(await result.json(), body, path);
  }
  ready = {status: 503, body: {status: 'unavailable'}};
  await probe('/livez', 200, {status: 'ok'});
  for (const path of ['/readyz', '/healthz']) await probe(path, 503, ready.body);
  ready = {status: 200, body: {status: 'degraded', generation: 1}};
  for (const path of ['/readyz', '/healthz']) await probe(path, 200, ready.body);
  for (const failure of ['error', 'not-ok', 'disconnected', 'stall']) {
    health = failure;
    const start = performance.now();
    await probe('/livez', 503, {status: 'unavailable'});
    assert.ok(performance.now() - start < 4500, 'liveness has a finite failure budget');
  }
  health = 'ok';
  unavailable = true;
  for (const path of ['/livez', '/readyz', '/healthz']) {
    const result = await fetch(`http://127.0.0.1:${port}${path}`);
    assert.equal(result.status, 503);
    assert.equal(result.headers.get('cache-control'), 'no-store');
  }
  unavailable = false;
  console.log('PASS health: liveness, readiness/compatibility, degraded, no snapshot, failure, timeout, no-store');
  const invalidSelection = await wire('/?monitor=not-registered');
  assert.equal(invalidSelection.status, 422);
  assert.match(invalidSelection.body.toString(), /Invalid filter selection/);
  const listing = await read('/');
  assert.match(listing, /aria-label="Monitors"/);
  assert.equal((listing.match(/<col(?:\s[^>]*)?\s*\/?>/g)||[]).length, 5);
  assert.doesNotMatch(listing, /<th[^>]*>Maintenance<\/th>/);
  assert.match(listing, /data-auto-submit/);
  assert.match(listing, /aria-describedby="filter-behavior"/);
  assert.equal((listing.match(/<form\b/g) || []).length, 1);
  assert.doesNotMatch(listing, /<select[^>]*name="(?:monitor|check)"/);
  const script = await wire('/filters.js');
  let filterChange, submissions = 0;
  const fallback = {hidden: false};
  class Select {}
  runInNewContext(script.body.toString(), {
    document: {querySelectorAll: () => [{
      addEventListener(event, listener) { assert.equal(event, 'change'); filterChange = listener; },
      requestSubmit() { submissions++; }, querySelector() { return fallback; },
    }]}, HTMLSelectElement: Select,
  });
  assert.equal(fallback.hidden, true);
  for (let i = 0; i < 5; i++) filterChange({target: new Select()});
  assert.equal(submissions, 5);
  filterChange({target: {}}); assert.equal(submissions, 5);
  const focused = await read('/?monitor=yanked&buildsystem=custom&build=rva23:blocked');
  assert.equal(lastQuery.get('monitor'), 'yanked');
  assert.deepEqual(lastQuery.getAll('build'), ['rva23:blocked']);
  assert.match(focused, /Release withdrawn/);
  assert.doesNotMatch(focused, /data-key="success"/);
  assert.equal((focused.match(/<col(?:\s[^>]*)?\s*\/?>/g)||[]).length, 2);
  const coverage = await read('/?monitor=yanked&buildsystem=custom&build=rva23:blocked&check=ok');
  assert.match(coverage, /Last checked/);
  assert.match(coverage, /Checked/);
  assert.equal((coverage.match(/<col(?:\s[^>]*)?\s*\/?>/g)||[]).length, 3);
  const nav = coverage.match(/<nav[^>]*aria-label="Monitors"[^]*?<\/nav>/)[0];
  for (const match of nav.matchAll(/href="([^"]+)"/g)) {
    const link = new URL(match[1].replaceAll('&amp;', '&'), 'http://fixture');
    assert.equal(link.searchParams.get('check'), null);
    assert.equal(link.searchParams.get('buildsystem'), 'custom');
    assert.deepEqual(link.searchParams.getAll('build'), ['rva23:blocked']);
  }
  const unknownPort = await read('/packages/yanked');
  assert.match(unknownPort, /Release withdrawn/);
  assert.match(unknownPort, />Files<\/dt>/);
  assert.match(unknownPort, />3<\/span>/);
  assert.doesNotMatch(unknownPort, /Review linked evidence|<th>Action|urgent/);
  const row = name => listing.match(new RegExp(`<tr data-key="${name}"[^]*?</tr>`))?.[0] ?? '';
  assert.match(row('success'), /data-appearance="buildsystem%3Acustom"/);
  assert.doesNotMatch(row('success'), /#build/);
  assert.equal((row('success').match(/>✓</g) || []).length, 3);
  for (const version of ['1.9', '1.8', '1.7']) assert.ok(row('failed').includes(`>${version}</a>`));
  assert.doesNotMatch(listing, /9\.9-shared|· last/);
  assert.match(row('failed'), /tone-negative/);
  assert.match(await read('/presentation.css'), /background-color:#123456/);
  assert.doesNotMatch(listing, / style=/);
  const detail = await read('/packages/failed');
  assert.match(detail, />\/SPECS\/failed<\/a>/);
  assert.doesNotMatch(detail, /SPEC Source:/);
  assert.match(detail, /Last successful version/);
  assert.match(detail, /2026-09-19 11:00:00 UTC/);
  const security = await read('/packages/security');
  assert.equal((security.match(/Queried source component only/g) || []).length, 1);
  assert.match(security, /0\.396%/);
  assert.match(security, /Reported fixes/);
  assert.match(security, /unavailable/);
  assert.match(security, />No<\/span>/);
  assert.doesNotMatch(security, /Review linked evidence|SecurityReview|urgent/);
  assert.match(security, /<section\b[^>]*id="security"[^>]*>/, 'evidence remains expanded');
  assert.equal((security.match(/>Observed version<\/dt>/g) || []).length, 1);
  assert.equal((security.match(/>Reported fixes<\/dt>/g) || []).length, 2);
  assert.doesNotMatch(detail, />Track<\/dt>|id="version"/);
  assert.doesNotMatch(listing, /<details|More filters/);
  assert.match(detail, /id="checks"/);
  const license = await read('/packages/license-evidence');
  assert.match(license, /MIT, Apache-2\.0/);
  assert.match(license, />Target<\/dt>/);
  assert.match(license, />No<\/span>/);
  assert.match(license, /<section\b[^>]*id="license"[^>]*>/, 'license evidence remains expanded');
  for (const pkg of packages) {
    const page = await read(pkg.detail_url);
    assert.match(page, /<section\b[^>]*id="changelog"[^>]*>/);
    assertCollapsedChecksLast(page);
  }
  assert.match(await read('/', 'theme=dark'), /data-theme="dark"/);
  assert.match(await read('/', 'theme=light'), /data-theme="light"/);
  // Unknown document data is escaped, including provenance URLs. No raw HTML or scripts.
  packages.push(makePackage('hostile', {spec: {source_path: 'SPECS/hostile', source_url: 'javascript:alert(1)',
    metadata: {summary: '<script>alert(1)</script>', url: '//evil.example'}, changelog: []}}));
  const hostile = await read('/packages/hostile');
  assert.match(hostile, /&lt;script&gt;/);
  assert.doesNotMatch(hostile, /href="(?:javascript:|\/\/evil)/);
  assertCollapsedChecksLast(hostile);
  packages.pop();
  console.log('PASS documents: arbitrary monitor, opaque navigation/facets, immediate GET, visible tables/fields, safe links, theme and retained evidence');
  // Wire-level tests deliberately bypass fetch's automatic decompression/cache.
  const identity = await wire('/');
  assert.equal(identity.status,200);assert.equal(identity.headers['content-encoding'],undefined);
  assert.equal(identity.headers['cache-control'],'private, no-cache');
  assert.match(identity.headers['content-security-policy'], /script-src 'self'/);
  assert.doesNotMatch(identity.headers['content-security-policy'], /unsafe-inline|unsafe-eval/);
  assert.match((await wire('/packages/success')).headers['content-security-policy'], /script-src 'none'/);
  assert.match(identity.headers.vary,/Cookie/);
  const etag=identity.headers.etag;assert.match(etag,/^W\/"[0-9a-f]{64}"$/);
  for (const coding of ['gzip','br']) {
    const compressed=await wire('/',{'Accept-Encoding':coding});
    assert.equal(compressed.headers['content-encoding'],coding);
    assert.match(compressed.headers.vary,/Accept-Encoding/);
    const decoded=(coding==='gzip'?gunzipSync:brotliDecompressSync)(compressed.body);
    assert.deepEqual(decoded,identity.body);
    assert.ok(compressed.body.length<identity.body.length/2);
    assert.equal(compressed.headers.etag,etag);
    console.log(`TRANSPORT ${coding}: ${identity.body.length} -> ${compressed.body.length} bytes`);
  }
  const prohibited=await wire('/',{'Accept-Encoding':'gzip;q=0, br;q=0, deflate;q=0, identity;q=1'});
  assert.equal(prohibited.headers['content-encoding'],undefined);assert.deepEqual(prohibited.body,identity.body);
  const validated=await wire('/',{'If-None-Match':etag,'Accept-Encoding':'gzip'});
  assert.equal(validated.status,304);assert.equal(validated.body.length,0);assert.equal(validated.headers.etag,etag);
  assert.equal(validated.headers['cache-control'],'private, no-cache');assert.match(validated.headers.vary,/Cookie/);
  assert.equal((await wire('/',{'If-None-Match':'"not-this", '+etag.slice(2)})).status,304);
  const themed=await wire('/',{'If-None-Match':etag,Cookie:'theme=dark'});assert.equal(themed.status,200);assert.notEqual(themed.headers.etag,etag);
  assert.equal((await wire('/?q=success',{'If-None-Match':etag})).status,200);
  packages[0].current='2.0.1';
  const changed=await wire('/',{'If-None-Match':etag});assert.equal(changed.status,200);assert.notEqual(changed.headers.etag,etag);
  packages[0].current='2.0';
  const cssPath=identity.body.toString().match(/href="(\/_astro\/[^"]+\.css)"/)[1];
  const css=await wire(cssPath,{'Accept-Encoding':'gzip'});assert.equal(css.status,200);assert.equal(css.headers['content-encoding'],'gzip');assert.match(css.headers['cache-control'],/immutable/);assert.ok(gunzipSync(css.body).length>css.body.length);
  const range=await wire(cssPath,{'Accept-Encoding':'gzip',Range:'bytes=0-9'});assert.equal(range.status,206);assert.equal(range.headers['content-encoding'],undefined);assert.equal(range.body.length,10);
  assert.equal((await wire('/',{},'HEAD')).body.length,0);
  const json=await wire('/api/v1/packages',{'Accept-Encoding':'gzip'});assert.equal(json.status,200);assert.equal(json.headers['cache-control'],'no-store');assert.equal(json.headers['content-encoding'],'gzip');assert.ok(JSON.parse(gunzipSync(json.body)).items.length>0);
  const redirect=await wire('/theme?to=dark&from=%2F');assert.equal(redirect.status,303);assert.equal(redirect.headers['cache-control'],'no-store');assert.ok(redirect.headers['set-cookie']);assert.equal(redirect.headers.etag,undefined);
  unavailable=true;
  const failure=await wire('/');assert.equal(failure.status,503);assert.equal(failure.headers['cache-control'],'no-store');assert.equal(failure.headers.etag,undefined);
  unavailable=false;
  console.log('PASS transport: gzip/br, identity/q=0, decoded equality, ETag304, changed data/theme/query, immutable CSS GET, ranges, HEAD, JSON, errors, cookies and CSP');
  console.log('PASS SSR document renderer');
} finally {
  child.kill('SIGTERM'); await once(child, 'exit'); mock.closeAllConnections(); await new Promise(resolve => mock.close(resolve));
}
