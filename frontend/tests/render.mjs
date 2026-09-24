// Render the production SSR bundle against deterministic API observations.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
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
  spec: {source_path: `SPECS/${name}`, source_url: `https://gitlab.example.org/team/packaging/-/tree/review/SPECS/${name}`, metadata: {summary: 'Fixture package', url: 'https://example.org/upstream'}, changelog: [], error: null},
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
    monitor: 'license', id: 'license-target', label: 'LicenseChange', title: 'Target license metadata',
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
// Production pages receive only the v2 monitor contract, never these flat fields.
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
function monitored(pkg, detail = true) {
  const check = {status: 'ok', stale: false, checked_at: '2026-09-19T11:10:00Z', attempted_at: null,
    error: null, note: null, changed_at: null, evidence_revision: null};
  const data = {
    source: {kind: 'source', version: pkg.current, revision: 'fixture', buildsystem: pkg.buildsystem,
      buildsystem_status: pkg.buildsystem_status, ...pkg.spec, obs: {}},
    version: {kind: 'version', current: pkg.current, latest: pkg.latest, relation: pkg.relation, track: pkg.track,
      track_label: pkg.track_label, stale: pkg.stale, error: pkg.version_error, last_known_relation: pkg.relation,
      updated_at: pkg.upstream_updated_at, upstream: {}, watch: pkg.watch || []},
    build: {kind: 'build', targets: pkg.builds, source_version: pkg.current,
      source_success: pkg.current_build_success, last_successful_version: pkg.last_successful_version},
  };
  const monitors = Object.fromEntries(catalog.map(m => [m.id, {id: m.id, title: m.title, check: {...check},
    data: data[m.id] || {kind: 'evidence', labels: m.id === 'yanked' ? pkg.maintenance : [],
      findings: pkg.maintenance_findings.filter(f => f.monitor === m.id)}}]));
  monitors.build.check.checked_at = pkg.builds.every(b => b.updated_at) ? pkg.builds[0].updated_at : null;
  if (!detail) {
    for (const result of Object.values(monitors)) {
      for (const key of ['findings', 'metadata', 'changelog', 'obs', 'upstream', 'watch']) delete result.data[key];
    }
  }
  return {name: pkg.name, detail_url: pkg.detail_url, monitors, presentation: pkg.presentation};
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
  if (url.pathname === '/api/v2/packages' && url.searchParams.get('monitor') === 'not-registered') {
    res.writeHead(422, {'Content-Type': 'application/json'}); res.end('{}'); return;
  }
  if (url.pathname === '/api/v2/packages') lastQuery = url.searchParams;
  const selected = packages.find(pkg => url.pathname === `/api/v2/packages/${pkg.name}`);
  const payload = url.pathname === '/api/v1/presentation'
    ? {buildsystems: {custom: {background: '#123456', foreground: '#ffffff'}}}
    : selected ? monitored(selected) : {
      monitors: catalog, check_statuses: {ok: packages.length},
      presentation: {buildsystems: {custom: {background: '#123456', foreground: '#ffffff'}}},
      buildsystems: {custom: 1}, maintenance_labels: {NewSignal: 1},
      build_statuses: Object.fromEntries(targets.map(target => [target.id, [{value:'issues',label:'Issues',count:2}, {value:'blocked',label:'Blocked',count:1}]])),
      items: (url.searchParams.get('q') === 'quiet' ? packages.map(pkg=>({...pkg,maintenance:[]})) : packages).map(pkg => monitored(pkg, false)),
      total: packages.length, page: 1, per_page: 100, pages: 1,
      counts: {all: packages.length, updates: 3, problems: 1, attention: 1, untracked: 1}, targets,
      collection: {obs_updated_at: '2026-09-19T11:10:00Z', upstream_updated_at: '2026-09-19T10:50:00Z', last_attempt: null, mode: 'live', errors: ['intentional fixture error'], generation: 1,
        packages: packages.length, tracked_packages: packages.length - 1}};
  res.writeHead(200, {'Content-Type': 'application/json'}); res.end(JSON.stringify(payload));
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
  assert.doesNotMatch(invalidSelection.body.toString(), /Snapshot unavailable/);
  const quiet = await read('/?q=quiet');
  assert.doesNotMatch(quiet, /class="maintenance-labels"/);
  assert.doesNotMatch(quiet, /<th scope="col">Maintenance<\/th>/);
  const listing = await read('/');
  assert.doesNotMatch(listing, /<th scope="col">Maintenance<\/th>/);
  assert.equal((listing.match(/<col(?:\s[^>]*)?\s*\/?>/g)||[]).length, 5);
  assert.doesNotMatch(listing, /Unverified|Some checks are incomplete|Collection status|Upstream checks cover/);
  assert.match(listing, />Untracked <b>/);
  assert.match(listing, /data-auto-submit/);
  const filterForms = [...listing.matchAll(/<form\b[^]*?<\/form>/g)];
  assert.equal(filterForms.length, 1, 'search and selections must share one GET form');
  const filterForm = filterForms[0][0];
  assert.match(filterForm, /name="q"/);
  assert.equal((filterForm.match(/<select\b/g) || []).length, targets.length + 3);
  assert.doesNotMatch(filterForm, /type="hidden" name="(?:q|buildsystem|maintenance|build)"/);
  assert.equal((filterForm.match(/aria-describedby="filter-behavior"/g) || []).length, targets.length + 3);

  assert.match(listing, /<script[^>]*src="\/filters.js"[^>]*defer/);
  const script = await wire('/filters.js');
  assert.equal(script.status, 200);
  let filterChange;
  let submissions = 0;
  const fallback = {hidden: false};
  class Select {}
  const form = {
    addEventListener(event, listener) { assert.equal(event, 'change'); filterChange = listener; },
    requestSubmit() { submissions += 1; },
    querySelector(selector) { assert.equal(selector, '[data-filter-submit]'); return fallback; },
  };
  runInNewContext(script.body.toString(), {
    document: {querySelectorAll: () => [form]}, HTMLSelectElement: Select,
  });
  assert.equal(fallback.hidden, true);
  for (let i = 0; i < 5; i += 1) filterChange({target: new Select()});
  assert.equal(submissions, 5, 'every filter selection submits immediately');
  filterChange({target: {}});
  assert.equal(submissions, 5);
  const fields = {maintenance: {value: 'Withdrawn'}, check: {value: 'error'}};
  form.querySelector = selector => fields[selector.match(/name="(.*?)"/)[1]];
  const monitorSelect = new Select(); monitorSelect.name = 'monitor';
  filterChange({target: monitorSelect});
  assert.equal(fields.maintenance.value, 'Withdrawn');
  assert.equal(fields.check.value, '');
  assert.equal(submissions, 6, 'switching monitor resets only monitor-local selections');
  const focused = await read('/?monitor=yanked&buildsystem=custom&build=rva23:blocked&check=ok');
  assert.equal(lastQuery.get('monitor'), 'yanked');
  assert.equal(lastQuery.get('check'), 'ok');
  assert.deepEqual(lastQuery.getAll('build'), ['rva23:blocked']);
  assert.match(focused, /value="yanked" selected/);
  assert.match(focused, /<th scope="col">Release files<\/th>/);
  assert.equal((focused.match(/<col(?:\s[^>]*)?\s*\/?>/g)||[]).length, 2);
  assert.match(focused, />No findings</);
  assert.match(focused, /maintenance=Withdrawn/);
  const sourceFocus = await read('/?monitor=source');
  assert.match(sourceFocus, /<th scope="col">Source<\/th>/);
  assert.match(sourceFocus, /class="current-version">2\.0/);
  const buildFocus = await read('/?monitor=build');
  assert.equal((buildFocus.match(/<col(?:\s[^>]*)?\s*\/?>/g)||[]).length, 4);
  for (const target of targets) assert.match(buildFocus, new RegExp(`<th scope="col"[^>]*>${target.label}</th>`));
  const unknownPort = await read('/packages/yanked');
  assert.match(unknownPort, /<h2>Release files<\/h2>/);
  assert.match(unknownPort, /Release withdrawn/);
  assert.match(unknownPort, /<dt>Files/);
  assert.match(unknownPort, />3<\/dd>/);
  assert.doesNotMatch(unknownPort, /Review linked evidence|<th>Action|urgent/);
  console.log('PASS composition: data-driven monitor selector, replaceable columns, generic unregistered renderer, independent checks, immediate linked GET selections');
  const row = name => listing.match(new RegExp(`<tr data-name="${name}"[^]*?</tr>`))?.[0] ?? '';
  assert.match(row('success'), /class="current-version"/);
  const linked = await read('/?build=rva23:blocked&build=rva20:issues&buildsystem=custom&maintenance=NewSignal&q=ok');
  assert.deepEqual(lastQuery.getAll('build'), ['rva23:blocked', 'rva20:issues']);
  for (const target of targets) assert.match(linked, new RegExp(`id="build-${target.id}"`));
  assert.match(linked, /value="rva23:blocked" selected/);
  const allLinks = [...linked.matchAll(/href="([^"]+)"/g)].map(match => new URL(match[1].replaceAll('&amp;', '&'), 'http://fixture'));
  for (const link of allLinks.filter(url => url.searchParams.get('buildsystem') === 'custom' || url.searchParams.get('maintenance') === 'NewSignal')) {
    assert.deepEqual(link.searchParams.getAll('build'), ['rva23:blocked', 'rva20:issues']);
  }
  assert.doesNotMatch(linked.match(/<nav class="filters"[^]*?<\/nav>/)[0], /Build issues/);
  await read('/?q=%20%20test%20%20&page=-20&view=invalid&build=rva23:&maintenance=NewSignal');
  assert.equal(lastQuery.get('q'), 'test');
  assert.equal(lastQuery.get('page'), '1');
  assert.equal(lastQuery.get('view'), 'all');
  assert.equal(lastQuery.get('maintenance'), 'NewSignal');
  assert.deepEqual(lastQuery.getAll('build'), []);
  await read('/?page=999999999');
  assert.equal(lastQuery.get('page'), '1000000');

  assert.match(row('success'), /buildsystem=custom/);
  assert.match(row('success'), /data-buildsystem="custom"/);
  assert.doesNotMatch(listing, / style=/);
  assert.match(await read('/presentation.css'), /background-color:#123456/);
  const presentationCSS = await wire('/presentation.css');
  assert.equal((await wire('/presentation.css', {'If-None-Match':presentationCSS.headers.etag})).status, 304);
  assert.match(row('success'), /maintenance=NewSignal/);
  assert.match(row('success'), /NewSignal/);
  assert.match(row('success').match(/<th scope="row">[^]*?<\/th>/)[0], /package-identity[^]*buildsystem-label[^]*maintenance-labels/);
  assert.ok(row('success').indexOf('class="pkg"') < row('success').indexOf('class="buildsystem-line"'));
  assert.doesNotMatch(row('ahead'), /buildsystem-label|maintenance-label/);
  assert.match(row('success'), /class="new">2\.1/);
  assert.doesNotMatch(row('success'), /class="pkg outdated"|class="last-success"/);
  assert.match(row('failed'), /class="current-version failed"/);
  const versionCell = name => row(name).match(/<td>[^]*?<\/td>/)?.[0] ?? '';
  assert.doesNotMatch(versionCell('failed'), /1\.9|1\.8|1\.7|9\.9-shared|class="last-success"|Stale|Unavailable/);
  for (const [target, version] of [['rva23', '1.9'], ['rva20', '1.8'], ['x86_64', '1.7']]) {
    const cell = row('failed').match(new RegExp(`<td class="build" data-target="${target}">[^]*?<\\/td>`))?.[0] ?? '';
    assert.ok(cell.includes(`>${version}</a>`), `${target} must show its own history: ${cell}`);
    assert.match(cell, />Failed</);
    assert.doesNotMatch(cell, /· last|>last /);
    assert.doesNotMatch(cell, /9\.9-shared/);
  }
  assert.match(row('success'), /class="build-observation compact"/);
  for (const name of ['success', 'long-version']) {
    assert.doesNotMatch(row(name), /class="build-version/);
    assert.equal((row(name).match(/>✓</g) || []).length, 3);
    assert.match(row(name), /Last succeeded: .*2026-09-19 11:00:00 UTC/);
  }
  for (const name of ['old-source', 'unknown-source', 'arch-version', 'failed-same-version']) {
    assert.equal((row(name).match(/class="build-version"/g) || []).length, 3, `${name}: do not infer current-source success`);
  }
  assert.match(row('arch-version'), />2\.0\.arch<\/a>/);
  assert.match(listing, /Last updated: <span>OBS <time datetime="2026-09-19T11:10:00Z">2026-09-19 11:10:00/);
  assert.match(listing, /Upstream <time datetime="2026-09-19T10:50:00Z">2026-09-19 10:50:00/);
  assert.doesNotMatch(listing, />Stale<| · stale|>Unavailable<|9\.9-shared/);
  assert.match(row('untracked'), /class="current-version untracked"/);
  assert.doesNotMatch(row('untracked'), /class="last-success"/);
  assert.match(row('untracked-unavailable-source'), /class="current-version untracked"/);
  assert.match(row('unknown'), /class="current-version unavailable"/);
  assert.doesNotMatch(versionCell('unknown'), />Unavailable<|>Stale</);
  assert.match(versionCell('unknown'), /cannot currently be compared/);
  assert.doesNotMatch(row('unknown'), />Untracked</);
  const security = await read('/packages/security');
  assert.equal((security.match(/Queried source component only/g) || []).length, 1);
  assert.match(security, /0\.396%/);
  assert.match(security, /<summary>Security 2/);
  assert.doesNotMatch(security, /<details[^>]* open|Observed version/);
  assert.equal((security.match(/class="security-query"/g) || []).length, 1);
  for (const id of ['CVE-2026-1001', 'CVE-2026-1002']) {
    assert.ok(security.includes(`https://nvd.nist.gov/vuln/detail/${id}`));
  }
  assert.match(security, /Fixed events/);
  assert.match(security, /Reported fixes/);
  assert.match(security, /<td>3\.0<\/td>/);
  assert.match(security, />No</);
  assert.match(security, /unavailable/);
  assert.doesNotMatch(security, /<th>Action<\/th>|SecurityReview|urgent/);
  assert.ok(!security.includes('Review linked evidence.'));
  const licenseEvidence = await read('/packages/license-evidence');
  assert.match(licenseEvidence, /maintenance=LicenseChange/);
  assert.match(licenseEvidence, /Target 2\.1/);
  assert.match(licenseEvidence, /MIT, Apache-2\.0/);
  assert.match(licenseEvidence, />unavailable<\/dd>/);
  assert.match(licenseEvidence, />No<\/dd>/);
  const detail = await read('/packages/failed');
  assert.doesNotMatch(detail, /Release track|class="track"|class="rel outdated"|>Outdated</);
  assert.match(detail, /class="new">2\.1/);
  assert.match(detail, /href="\/api\/v2\/packages\/failed">Raw data \(JSON\)<\/a>/);
  assert.doesNotMatch(detail, /source version and revision, upstream observation/);
  assert.match(await read('/packages/ahead'), /class="rel ahead">Ahead<\/span>/);
  const successDetail = await read('/packages/success');
  assert.doesNotMatch(successDetail, /Explicitly watched tracks|>Watching</);
  const watchDetail = await read('/packages/watch-preview');
  assert.match(watchDetail, /aria-label="Explicitly watched tracks"/);
  assert.match(watchDetail, /href="\/api\/v1\/tracks\/widget%40preview"/);
  assert.match(watchDetail, /<strong>2\.2rc1<\/strong>/);
  assert.match(watchDetail, /2\.3dev1<\/strong>[^]*?\(last observed\)/);
  assert.match(watchDetail, /widget@missing<\/a>: <strong>—<\/strong>/);
  assert.match(watchDetail, /class="new">2\.1/); // Watch never replaces the release comparison.
  assert.doesNotMatch(row('watch-preview'), /2\.2rc1|2\.3dev1|>Watching</);

  assert.equal((successDetail.match(/<strong>2\.0<\/strong>/g) || []).length, 3);
  assert.match(successDetail, /datetime="2026-09-19T11:00:00\+00:00"/);
  assert.match(detail, /href="https:\/\/gitlab\.example\.org\/team\/packaging\/-\/tree\/review\/SPECS\/failed">\/SPECS\/failed<\/a>/);
  assert.ok(detail.indexOf('<dt>SPEC Source</dt>') < detail.indexOf('<dt>Upstream</dt>'));
  assert.match(detail, />Last successful version<\/th>/);
  assert.match(detail, /datetime="2026-09-19T11:00:00Z">2026-09-19 11:00:00<\/time>/);
  assert.match(await read('/packages/untracked-unavailable-source'), /class="rel untracked">Untracked<\/span>/);
  assert.match(await read('/packages/untracked'), /class="rel untracked">Untracked<\/span>/);
  const missing = await read('/packages/missing-history');
  assert.match(missing, /<strong title="No last-success record is available for this observation.">—<\/strong>/);
  assert.match(missing, /OBS —<\/span>/);
  assert.match(detail, /OBS <time datetime="2026-09-19T11:10:00Z">2026-09-19 11:10:00/);
  assert.match(detail, /Last updated: 2026-09-19 11:10:00 UTC/);
  assert.doesNotMatch(detail, /<th scope="col">Last updated/);
  assert.doesNotMatch(await read('/packages/untracked'), /Last updated:[^]*?Upstream <time/);
  const unresolved = await read('/packages/unresolved-version');
  assert.match(unresolved, /<strong title="OBS recorded a successful build, but its version could not be resolved.">—<\/strong>/);
  assert.doesNotMatch(unresolved, />Version unavailable</);
  assert.doesNotMatch(row('unresolved-version'), /class="build-version/);
  assert.doesNotMatch(row('multibuild'), /class="build-version/);
  assert.match(row('multibuild'), /href="\/packages\/multibuild"/);
  assert.doesNotMatch(row('multibuild'), />1\.1<|>1\.0</);
  assert.match(unresolved, /datetime="2026-09-19T11:00:00Z"/);
  const flavors = await read('/packages/multibuild');
  assert.match(flavors, />multibuild:one<\/span>/);
  assert.match(flavors, />multibuild:two<\/span>/);
  assert.match(flavors, /<strong>1\.1<\/strong>/);
  assert.match(flavors, /<strong>1\.0<\/strong>/);
  assert.match(await read('/', 'theme=dark'), /data-theme="dark"/);
  assert.match(await read('/', 'theme=light'), /data-theme="light"/);
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
  console.log('PASS SSR: inferred current-success duplicates omitted, exception histories retained, arrow replaces Outdated, concise raw-data link; per-target histories, clean source version, observation times, no false missing/multi-flavor values, SPEC links, light/dark themes');
} finally {
  child.kill('SIGTERM'); await once(child, 'exit'); mock.closeAllConnections(); await new Promise(resolve => mock.close(resolve));
}
