// Render the production SSR bundle against deterministic API observations.
import assert from 'node:assert/strict';
import {spawn, spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {once} from 'node:events';
import {createServer, request} from 'node:http';
import {gunzipSync, brotliDecompressSync} from 'node:zlib';

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
  makePackage('security', {version_annotations: [{monitor:'security',label:'Advisory',count:8,scope:'current',target_version:null,stale:false,finding_ids:[]}], maintenance_findings: [1, 2, 3, 4, 5, 6, 7, 8].map(i => ({
    monitor: 'security', id: `CVE-2026-100${i}`, label: 'Advisory', title: `CVE-2026-100${i}`,
    facts: [{key: 'Observed version', code: 'query', value: '2.0', source: 'OSV', url: 'https://api.osv.dev/v1/query', status: 'observed'},
      {key: 'Exploit probability', code: 'epss_probability', value: 0.00396, source: 'FIRST', url: 'https://api.first.org/data/v1/epss', status: 'observed'},
      {key: 'Reported fixes', code: 'fixed_events', value: ['3.0'], source: 'OSV', url: 'https://osv.dev/vulnerability/fixture', status: 'observed'},
      {key: 'KEV', value: i === 1 ? false : null, source: 'CISA', url: 'https://www.cisa.gov/known-exploited-vulnerabilities-catalog', status: i === 1 ? 'observed' : 'unavailable'}],
    evidence_url: `https://nvd.nist.gov/vuln/detail/CVE-2026-100${i}`,
    scope: 'current', tags: [], stale: false,
  }))}),
  makePackage('license-evidence', {version_annotations: [{monitor:'license',label:'LicenseDiff',count:1,scope:'upgrade',target_version:'2.1',stale:false,finding_ids:[]}], maintenance_findings: [{
    monitor: 'license', id: 'license-target', label: 'LicenseDiff', title: 'MIT → Apache-2.0',
    evidence_url: 'https://example.org/license', scope: 'upgrade', target_version: '2.1',
    tags: [], stale: false, facts: [
      {key: 'Licenses', value: ['MIT', 'Apache-2.0'], source: 'Registry', url: 'https://example.org/license', status: 'observed'},
      {key: 'Unavailable field', value: null, source: 'Registry', url: 'https://example.org/license', status: 'unavailable'},
      {key: 'False field', value: false, source: 'Registry', url: 'https://example.org/license', status: 'observed'},
    ],
  }]}),
  makePackage('success', {buildsystem: 'custom', buildsystem_status: 'declared', maintenance: [{label:'NewSignal', count:2, stale:false}],
    spec: {...makePackage('success').spec, metadata: {summary: 'Fixture package',
      url: 'https://example.org/upstream', license: 'MIT', description: 'Fixture source description'}}}),
  makePackage('watch-preview', {watch: [
    {id:'widget@preview',version:'2.2rc1',error:null,stale:false},
    {id:'widget@nightly',version:'2.3dev1',error:'fetch failed',stale:false},
    {id:'widget@missing',error:'no result',stale:true},
  ]}),
  makePackage('ahead', {relation: 'ahead'}),
  makePackage('check-error', {checks: {version: {status: 'error', error: 'Fixture provider timeout'}}}),
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
  {id: 'security', title: 'Advisory', kind: 'evidence'},
  {id: 'license', title: 'LicenseDiff', kind: 'evidence'},
  {id: 'yanked', title: 'Release files', kind: 'evidence'},
  {id: 'requires', title: 'Dependencies', kind: 'requires'},
  {id: 'eol', title: 'EOL', kind: 'evidence'},
];
// Synthetic runtime assessments, not live registry versions or host evaluation.
const constraint = (expression, release) => ({expression, source: 'Fixture registry',
  url: `https://example.org/fixture/${release}/requirements`});
const observedDependency = version => ({version, revision: 'fixture-source', origin: 'spec',
  checked_at: '2026-09-19T11:00:00Z', stale: false});
const runtimeRequirement = (dependency, patch = {}) => ({dependency, name: dependency, kind: 'runtime',
  scheme: 'pep440', identity: null, condition: null, extras: [], optional: false, package: dependency, mapping: 'mapped',
  current: constraint('>=1.0', 'current'), target: constraint('>=2.0', 'target'),
  observed: observedDependency('2.1'), satisfaction: 'satisfied', reason: null,
  target_satisfaction: 'satisfied', target_reason: null, changed: true, ...patch});
packages.push(makePackage('requires-upgrade', {requirements: [
  runtimeRequirement('python', {name: 'Python', current: constraint('>=3.8', 'current'),
    target: constraint('>=3.10', 'target'), observed: observedDependency('3.11.8')}),
  runtimeRequirement('libwidget', {scheme: 'rpm_version', observed: observedDependency('0.9'),
    satisfaction: 'unsatisfied', target_satisfaction: 'unsatisfied'}),
  runtimeRequirement('optional-library', {name: 'Optional library', optional: true,
    condition: '(platform_python_implementation == "CPython" and sys_platform != "android") and extra == "speedups"',
    satisfaction: 'unknown', reason: 'condition_not_evaluated',
    target_satisfaction: 'unknown', target_reason: 'condition_not_evaluated'}),
  runtimeRequirement('libexample', {scheme: 'numeric_minimum', current: constraint('1.80', 'current'),
    target: constraint('1.82', 'target'), observed: observedDependency('1.81.0'), target_satisfaction: 'unsatisfied'}),
]}));
packages.push(makePackage('requires-current', {latest: '2.0', relation: 'current', requirements: [
  runtimeRequirement('libcurrent', {target: null, target_satisfaction: 'unknown',
    target_reason: 'requirement_not_observed', changed: false}),
]}));
packages.push(makePackage('requires-target-only', {requirements: [
  runtimeRequirement('libtarget', {current: null, observed: observedDependency('1.9'),
    satisfaction: 'unknown', reason: 'requirement_not_observed', target_satisfaction: 'unsatisfied', changed: false}),
]}));
packages.push(makePackage('build-reason', {builds: makePackage('base').builds.map((b, index) => ({...b,
  raw_status: index === 0 ? 'unresolvable' : 'building', text: index === 0 ? 'Unresolvable' : 'Building',
  issue: index === 0, kind: 'pending', details: index === 0 ? 'nothing provides <fixture-dependency>' : 'worker://internal-host'}))}));
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
      annotations: pkg.version_annotations || [], track_label: pkg.track_label, stale: pkg.stale, error: pkg.version_error, last_known_relation: pkg.relation,
      updated_at: pkg.upstream_updated_at, upstream: {}, watch: pkg.watch || []},
    build: {kind: 'build', targets: targets.map(t => pkg.builds.find(b => b.target === t.id) || {...makePackage('base').builds.find(b => b.target === t.id), text: 'No result', raw_status: 'unknown', kind: 'muted', matches_source: null, last_success: null}), source_version: pkg.current,
      source_success: pkg.current_build_success, last_successful_version: pkg.last_successful_version},
    requires: {kind: 'requires', current_version: pkg.current, target_version: pkg.latest,
      requirements: pkg.requirements || [], findings: [], labels: [], finding_count: (pkg.requirements || []).length},
  };
  const monitors = Object.fromEntries(catalog.map(m => [m.id, {id: m.id, title: m.title, check: {...check},
    data: data[m.id] || {kind: 'evidence', labels: m.id === 'yanked' ? pkg.maintenance : [],
      findings: pkg.maintenance_findings.filter(f => f.monitor === m.id)}}]));
  monitors.version.dimensions = {view: !pkg.track ? ['untracked'] : ['outdated', 'changed'].includes(pkg.relation) ? ['updates'] : []};
  monitors.version.dimensions.maintenance = monitors.version.dimensions.view.map(value =>
    ({untracked: 'Untracked', updates: 'Outdated'})[value]);
  for (const [id, failure] of Object.entries(pkg.checks || {})) Object.assign(monitors[id].check, failure);
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
    result.data.entries = result.id === focus ? findings.map(({id, title, evidence_url, stale, scope, target_version, tags}) =>
      ({id, title, evidence_url, stale, scope, target_version, tags})) : [];
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
let appearancePalette = {custom: {background: '#123456', foreground: '#ffffff', icon: 'gopher'}};
let retainedCount = 0;
const publicPaths = [
  '/api/v2/packages', '/api/v2/packages/security', '/api/v2/tracks/widget',
  '/api/v2/targets', '/api/v2/status', '/api/v2/export', '/api/v2/packages:batchGet',
];
const forwardedRequests = [];
const mock = createServer((req, res) => {
  forwardedRequests.push(req.url);
  if (unavailable) { res.writeHead(503, {'Content-Type':'application/json'});res.end('{}');return; }
  const url = new URL(req.url, 'http://localhost');
  if (publicPaths.includes(url.pathname) && url.searchParams.has('proxy_fixture')) {
    const status = Number(url.searchParams.get('fixture_status')) || 200;
    const headers = {'Content-Type': 'application/json'};
    if (url.pathname === '/api/v2/export') headers['Content-Disposition'] = 'attachment; filename="fixture.json"';
    res.writeHead(status, headers);
    res.end(JSON.stringify({path: url.pathname, query: [...url.searchParams]})); return;
  }
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
  const evidenceFocus = catalog.some(m => m.id === focus && ['evidence', 'requires'].includes(m.kind));
  const resultRows = packages.filter(pkg => pkg.maintenance_findings.some(f => f.monitor === focus)
    || (focus === 'requires' && pkg.requirements?.length));
  const rows = evidenceFocus && section === 'results' ? resultRows : packages;
  const perPage = Math.max(1, Math.min(200, Number(url.searchParams.get('per_page')) || 100));
  const pages = Math.max(1, Math.ceil(rows.length / perPage));
  const page = Math.max(1, Math.min(pages, Number(url.searchParams.get('page')) || 1));
  const payload = url.pathname === '/api/ui/theme'
    ? {buildsystems: appearancePalette}
    : selected ? monitored(selected) : {
      monitors: catalog, check_statuses: {ok: packages.length}, section,
      result_count: resultRows.length, coverage_count: packages.length, retained_count: retainedCount,
      presentation: {buildsystems: appearancePalette},
      buildsystems: {custom: 1}, maintenance_labels: {CheckFailed: 1, NewSignal: 1, LicenseDiff: 1, DepMismatch: 1},
      requires_counts: {all: 3, unmet: 1, changes: 1},
      version_signals: {security: 1, license: 1, requires: 1},
      build_statuses: Object.fromEntries(targets.map(target => [target.id, [{value:'failed',label:'Failed',count:2}, {value:'blocked',label:'Blocked',count:1}]])),
      items: (url.searchParams.get('q') === 'quiet' ? rows.map(pkg=>({...pkg,maintenance:[],maintenance_findings:[]})) : rows).slice((page - 1) * perPage, page * perPage).map(pkg => monitored(pkg, true, focus)),
      total: rows.length, page, per_page: perPage, pages,
      counts: {all: packages.length, updates: 3, problems: 1, attention: 1, untracked: 1}, targets,
      collection: {build_service_url: 'https://build.example.org', source_repository: {url: 'https://github.com/fixture/repo.git', branch: 'stable/3', revision: 'abcdef0123456789abcdef0123456789abcdef01'}, obs_updated_at: '2026-09-19T11:10:00Z', upstream_updated_at: '2026-09-19T10:50:00Z', last_attempt: null, mode: 'live', errors: ['intentional fixture error'], generation: 1,
        packages: packages.length, tracked_packages: packages.length - 1}};
  const query = Object.fromEntries(url.searchParams);
  query.build = url.searchParams.getAll('build');
  query.maintenance = url.searchParams.getAll('maintenance');
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
  assert.ok(details.every(([, attrs]) => /id="(?:checks|requires-conditions)"/.test(attrs)),
    'only check diagnostics and dependency condition programs are collapsible');
  const checks = details.find(([, attrs]) => /id="checks"/.test(attrs));
  const [whole, attributes, body] = checks;
  assert.doesNotMatch(attributes, /(?:^|\s)open(?:\s|=|$)/, 'Checks starts closed');
  assert.match(body, /^\s*<summary\b[^>]*>Checks<\/summary>/);
  assert.match(body, /Collection checks/);
  assert.match(html.slice(checks.index + whole.length), /^\s*<\/div>/, 'Checks is the final main section');
  const changelog = html.match(/<section\b[^>]*id="changelog"[^>]*>/);
  if (changelog) assert.ok(changelog.index < checks.index, 'Checks follows Changelog');
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
  assert.equal((await wire('/healthz')).status, 404);
  ready = {status: 503, body: {status: 'unavailable'}};
  await probe('/livez', 200, {status: 'ok'});
  await probe('/readyz', 503, ready.body);
  ready = {status: 200, body: {status: 'degraded', generation: 1}};
  await probe('/readyz', 200, ready.body);
  for (const failure of ['error', 'not-ok', 'disconnected', 'stall']) {
    health = failure;
    await probe('/livez', 503, {status: 'unavailable'});
  }
  health = 'ok';
  unavailable = true;
  for (const path of ['/livez', '/readyz']) {
    const result = await fetch(`http://127.0.0.1:${port}${path}`);
    assert.equal(result.status, 503);
    assert.equal(result.headers.get('cache-control'), 'no-store');
  }
  unavailable = false;
  console.log('PASS health: liveness, readiness, degraded, no snapshot, failure, timeout, no-store');
  for (const path of publicPaths) {
    const query = '?proxy_fixture=1&build=rva23%3Afailed&build=rva20%3Ablocked&q=with%20space&names=first&names=second&include=version&include=security';
    const result = await wire(path + query);
    assert.equal(result.status, 200, path);
    assert.deepEqual(JSON.parse(result.body), {path, query: [...new URLSearchParams(query)]});
    if (path.endsWith('/export')) assert.equal(result.headers['content-disposition'], 'attachment; filename="fixture.json"');
  }
  for (const status of [404, 422, 503]) {
    const result = await wire('/api/v2/status?proxy_fixture=1&fixture_status=' + status);
    assert.equal(result.status, status, 'proxy preserves upstream status');
  }
  for (const path of ['/api/v1/status', '/api/v1/packages', '/api/v2/internal', '/api/v2/packages/security/extra']) {
    const count = forwardedRequests.length;
    assert.equal((await wire(path)).status, 404, path);
    assert.equal(forwardedRequests.length, count, 'rejected route must not contact backend');
  }
  console.log('PASS API proxy: v2 routes, query, status, export header, rejected paths');
  const apiReference = await read('/api');
  const article = apiReference.match(/<article\b[^>]*>([^]*?)<\/article>/)[1];
  assert.match(article, /href="\/openapi\.json"/);
  assert.match(article, /GET \/api\/v2/);
  const examples = [...article.matchAll(/<pre\b[^>]*><code\b[^>]*><a\b[^>]*href="([^"]+)"/g)]
    .map(([, href]) => new URL(href.replaceAll('&amp;', '&'), 'http://localhost'));
  assert.equal(examples.length, 2);
  assert.equal(examples[0].pathname, '/api/v2/packages');
  assert.equal(examples[0].searchParams.get('search'), 'observations');
  assert.equal(examples[0].searchParams.get('detail'), 'full');
  assert.equal(examples[1].pathname, '/api/v2/packages:batchGet');
  const names = examples[1].searchParams.getAll('names');
  assert.equal(names.length, 2);
  assert.equal(new Set(names).size, names.length);
  assert.ok(names.every(Boolean));
  assert.deepEqual(examples[1].searchParams.getAll('include'), ['version', 'security']);
  console.log('PASS API reference: OpenAPI, endpoint table and query links');
  const invalidSelection = await wire('/?monitor=not-registered');
  assert.equal(invalidSelection.status, 422);
  assert.match(invalidSelection.body.toString(), /Invalid filter selection/);
  const listing = await read('/');
  assert.match(listing, /<title>openRuyi Tracker<\/title>/);
  assert.match(listing, /class="brand"[^]*?<span>openRuyi Tracker<\/span>/);
  assert.match(listing, /lang="en"/);
  const globalNavigation = listing.match(/<div class="global-navigation"[^]*?<\/section>\s*<\/div>/)?.[0];
  assert.ok(globalNavigation, 'global filters have a persistent sidebar location');
  assert.doesNotMatch(globalNavigation, /<details\b|<summary\b/);
  assert.match(globalNavigation, /<h2>BuildSystem<\/h2>/);
  assert.match(globalNavigation, /aria-label="BuildSystem"/);
  assert.match(globalNavigation, /buildsystem=custom[^]*?>custom<\/span><\/span>\s*<b>1<\/b>/);
  assert.equal((globalNavigation.match(/class="brand-icon/g) || []).length, 1);
  const logoURL = globalNavigation.match(/<img src="([^"]+)"/)[1];
  assert.match(logoURL, /^\/_astro\/gopher[.\w-]*\.svg$/);
  const iconResponse = await wire(logoURL);
  assert.equal(iconResponse.status, 200);
  assert.match(iconResponse.headers['content-type'], /image\/svg\+xml/);
  assert.match(listing, /href="https:\/\/build.example.org\/">BuildService<\/a>/);
  assert.match(listing, /href="https:\/\/github.com\/fixture\/repo\/tree\/stable%2F3">stable\/3<\/a>/);
  assert.match(listing, /href="https:\/\/github.com\/fixture\/repo\/commit\/abcdef0123456789abcdef0123456789abcdef01"[^>]*>abcdef<\/a>/);
  assert.equal((listing.match(/<col(?:\s[^>]*)?\s*\/?>/g)||[]).length, 5);
  assert.equal((listing.match(/<form\b/g) || []).length, 1);
  const packageMenu = listing.match(/<nav[^>]*aria-label="Alerts"[^]*?<\/nav>/)[0];
  assert.match(packageMenu, />Outdated<\/span>/);
  assert.match(packageMenu, />Untracked<\/span>/);
  assert.match(packageMenu, /maintenance=Outdated/);
  assert.match(packageMenu, /maintenance=Untracked/);
  assert.match(packageMenu, /maintenance=CheckFailed/);
  assert.match(packageMenu, />DepMismatch<\/span>[^]*?>DepChanges<\/span>/);
  assert.match(packageMenu, /signal=requires/);
  const changedPage = await read('/?signal=requires');
  assert.match(changedPage, /aria-label="Remove DepChanges"/);
  assert.match(changedPage, /name="signal" value="requires"/);
  const untrackedRow = listing.match(/<tr data-key="untracked"[^]*?<\/tr>/)[0];
  assert.match(untrackedRow, /data-decoration="dashed"/);
  assert.match(untrackedRow, /label%3AUntracked[^>]*>Untracked/);
  assert.match(listing, /href="\/\?page=1&amp;maintenance=CheckFailed"/);
  const errorPage = await read('/packages/check-error');
  assert.match(errorPage, /id="check-version"/);
  assert.match(errorPage, /Fixture provider timeout/);
  assertCollapsedChecksLast(errorPage);
  assert.equal((packageMenu.match(/>LicenseDiff<\/span>/g) || []).length, 1);
  assert.match(packageMenu, /maintenance=LicenseDiff[^]*?>LicenseDiff<\/span><\/span>\s*<b>1<\/b>/);
  assert.equal((listing.match(/<button[^>]*type="submit"/g) || []).length, 1);
  const scoped = await read('/?buildsystem=custom&maintenance=LicenseDiff&build=rva23:blocked');
  for (const label of ['BuildSystem: custom', 'LicenseDiff', 'rva23: Blocked']) {
    assert.ok(scoped.includes(`aria-label="Remove ${label}"`));
  }
  const chips = scoped.match(/<div class="active-filters"[^]*?<\/div>/)[0];
  const clearBuild = chips.match(/<a href="([^"]+)" aria-label="Remove rva23: Blocked"/)[1];
  const cleared = new URL(clearBuild.replaceAll('&amp;', '&'), 'http://fixture').searchParams;
  assert.deepEqual(cleared.getAll('build'), []);
  assert.equal(cleared.get('maintenance'), 'LicenseDiff');
  assert.equal(cleared.get('buildsystem'), 'custom');
  const selectedMenu = scoped.match(/<nav[^>]*aria-label="Alerts"[^]*?<\/nav>/)[0];
  assert.match(selectedMenu, /aria-current="page"[^]*?>LicenseDiff<\/span>/);
  const searchForm = scoped.match(/<form\b[^]*?<\/form>/)[0];
  assert.match(searchForm, /name="maintenance" value="LicenseDiff"/);
  assert.match(searchForm, /name="build" value="rva23:blocked"/);
  const combined = await read('/?maintenance=LicenseDiff&maintenance=DepMismatch&signal=requires');
  for (const label of ['LicenseDiff', 'DepMismatch', 'DepChanges']) {
    assert.ok(combined.includes(`aria-label="Remove ${label}"`));
  }
  const combinedSearch = combined.match(/<form\b[^]*?<\/form>/)[0];
  assert.match(combinedSearch, /name="maintenance" value="LicenseDiff"/);
  assert.match(combinedSearch, /name="maintenance" value="DepMismatch"/);
  assert.match(combinedSearch, /name="signal" value="requires"/);
  const exactCheck = await read('/?monitor=security&check=expired');
  assert.match(exactCheck, /aria-label="Remove Check: Stale"/);
  const paged = await read('/?q=fixture&per_page=3&page=2&buildsystem=custom');
  const topPager = paged.match(/<nav[^>]*aria-label="Pages above results"[^]*?<\/nav>/)[0];
  const bottomPager = paged.match(/<nav[^>]*aria-label="Pages below results"[^]*?<\/nav>/)[0];
  const pagerLinks = html => [...html.matchAll(/href="([^"]+)"/g)].map(match => match[1]);
  assert.deepEqual(pagerLinks(topPager), pagerLinks(bottomPager));
  assert.equal(pagerLinks(topPager).length, 2);
  assert.ok(paged.indexOf(topPager) < paged.indexOf('<table class="data-table'));
  assert.ok(paged.indexOf(bottomPager) > paged.lastIndexOf('</table>'));
  for (const link of pagerLinks(topPager)) {
    const query = new URL(link.replaceAll('&amp;', '&'), 'http://fixture').searchParams;
    assert.equal(query.get('q'), 'fixture');
    assert.equal(query.get('buildsystem'), 'custom');
    assert.equal(query.get('per_page'), '3');
  }
  assert.doesNotMatch(listing, /Pages above results/);
  assert.equal((listing.match(/class="result-count"/g) || []).length, 2);
  assert.match(listing, /class="results-toolbar"[^]*?class="result-count"[^]*?<form[^>]*role="search"/);
  assert.match(listing, /rel="icon" type="image\/svg\+xml" href="\/openruyi.svg"/);
  assert.match(listing, /<img src="\/openruyi.svg" width="38" height="28" alt=""/);
  assert.match(await read('/openruyi.svg'), /<svg/);
  assert.match(await read('/about'), /CC BY-SA 4.0/);
  const simple = await read('/?monitor=version');
  assert.match(simple, /<table class="data-table"/);
  assert.match(await read('/?monitor=build'), /<table class="data-table wide status-matrix"/);
  assert.match(listing, /<table class="data-table wide"/);
  const sourceCoverage = await read('/?monitor=source&section=results&buildsystem=custom&build=rva23:blocked&per_page=2');
  const sourceTable = sourceCoverage.match(/<table\b[^]*?<\/table>/)[0];
  assert.match(sourceTable, />Check<\/th>/);
  assert.match(sourceTable, />Last checked<\/th>/);
  assert.match(sourceTable, /Checked/);
  assert.doesNotMatch(sourceTable, />(?:Source|Version)<\/th>|→/);
  assert.equal((sourceTable.match(/<col(?:\s[^>]*)?\s*\/?>/g) || []).length, 3);
  assert.match(sourceCoverage, /name="monitor" value="source"/);
  assert.match(sourceCoverage, /name="section" value="coverage"/);
  assert.equal(lastQuery.get('monitor'), 'source');
  assert.deepEqual(lastQuery.getAll('build'), ['rva23:blocked']);
  // The website forwards the URL; the backend presenter owns the reading scope.
  const focused = await read('/?monitor=yanked&buildsystem=custom&build=rva23:blocked');
  assert.equal(lastQuery.get('monitor'), 'yanked');
  assert.deepEqual(lastQuery.getAll('build'), ['rva23:blocked']);
  assert.match(focused, /Release withdrawn/);
  assert.doesNotMatch(focused, /data-key="success"/);
  assert.match(focused, /name="buildsystem" value="custom"/, 'search preserves the global filter');
  const focusedGlobal = focused.match(/<nav[^>]*aria-label="BuildSystem"[^]*?<\/nav>/)[0];
  assert.match(focusedGlobal, /aria-current="page"[^>]*data-appearance="buildsystem%3Acustom"[^]*?>custom<\/span>/);
  for (const match of focusedGlobal.matchAll(/href="([^"]+)"/g)) {
    const link = new URL(match[1].replaceAll('&amp;', '&'), 'http://fixture');
    assert.equal(link.searchParams.get('monitor'), 'yanked');
    assert.deepEqual(link.searchParams.getAll('build'), []);
  }
  assert.equal((focused.match(/<col(?:\s[^>]*)?\s*\/?>/g)||[]).length, 2);
  const coverage = await read('/?monitor=yanked&buildsystem=custom&build=rva23:blocked&check=ok');
  const coverageTable = coverage.match(/<table\b[^]*?<\/table>/)[0];
  assert.match(coverageTable, />Version<\/th>/);
  assert.doesNotMatch(coverageTable, /Last checked|>Checked</);
  assert.match(coverage, /Checked/);
  assert.equal((coverageTable.match(/<col(?:\s[^>]*)?\s*\/?>/g)||[]).length, 2);
  const nav = coverage.match(/<nav[^>]*aria-label="Monitors"[^]*?<\/nav>/)[0];
  for (const match of nav.matchAll(/href="([^"]+)"/g)) {
    const link = new URL(match[1].replaceAll('&amp;', '&'), 'http://fixture');
    assert.equal(link.searchParams.get('check'), null);
    assert.equal(link.searchParams.get('buildsystem'), 'custom');
    assert.deepEqual(link.searchParams.getAll('build'), []);
  }
  const unknownPort = await read('/packages/yanked');
  assert.match(unknownPort, /Release withdrawn/);
  assert.match(unknownPort, />Files<\/dt>/);
  assert.match(unknownPort, />3<\/span>/);
  const row = name => listing.match(new RegExp(`<tr data-key="${name}"[^]*?</tr>`))?.[0] ?? '';
  assert.match(row('success'), /data-appearance="buildsystem%3Acustom"/);
  assert.doesNotMatch(row('success'), /#build/);
  assert.equal((row('success').match(/>✓</g) || []).length, 3);
  for (const version of ['1.9', '1.8', '1.7']) assert.ok(row('failed').includes(`>${version}</a>`));
  assert.doesNotMatch(listing, /9\.9-shared/);
  assert.match(row('failed'), /tone-negative/);
  const presentationCSS = await read('/presentation.css');
  assert.match(presentationCSS, /\[data-appearance="buildsystem%3Acustom"\]\{--appearance-background:#123456;--appearance-foreground:#ffffff\}/);
  assert.doesNotMatch(presentationCSS, /\.value-tag|\.document-nav/, 'one palette authority serves every document role');
  appearancePalette = {custom: {background: '#654321', foreground: '#eeeeee'},
    'unsafe\"}body{color:red}': {background: '#123456;display:none', foreground: '#ffffff'}};
  const changedPaletteCSS = await read('/presentation.css');
  assert.match(changedPaletteCSS, /--appearance-background:#654321;--appearance-foreground:#eeeeee/);
  assert.doesNotMatch(changedPaletteCSS, /unsafe|body|display|#123456/);
  appearancePalette = {custom: {background: '#123456', foreground: '#ffffff', icon: 'rust'}};
  const changedIcon = await read('/');
  assert.match(changedIcon, /<img src="\/_astro\/rust[.\w-]*\.svg"/);
  for (const icon of ['not-in-catalog', '../gopher', 'https://untrusted.example/logo.svg']) {
    appearancePalette.custom.icon = icon;
    const fallback = (await read('/')).match(/<nav[^>]*aria-label="BuildSystem"[^]*?<\/nav>/)[0];
    assert.doesNotMatch(fallback, /<img|untrusted|not-in-catalog/);
    assert.match(fallback, />custom<\/span>/);
  }
  appearancePalette = {custom: {background: '#123456', foreground: '#ffffff', icon: 'gopher'}};
  assert.doesNotMatch(listing, / style=/);
  const sourceContext = await read('/packages/success');
  assert.match(sourceContext, /Fixture package/);
  assert.match(sourceContext, /Fixture source description/);
  assert.match(sourceContext, />MIT<\/span>/);
  assert.match(sourceContext, />\/SPECS\/success<\/a>/);
  assert.match(sourceContext, /href="https:\/\/example.org\/upstream"/);
  assert.match(sourceContext, /data-appearance="buildsystem%3Acustom"/);
  assert.match(sourceContext, /id="changelog"/);
  const sourceCheck = sourceContext.match(/<tr data-key="source"[^]*?<\/tr>/)[0];
  assert.match(sourceCheck, />Source</);
  assert.match(sourceCheck, /2026-09-19 11:10:00 UTC/);
  const detail = await read('/packages/failed');
  assert.match(detail, />\/SPECS\/failed<\/a>/);
  assert.match(detail, /Last successful version/);
  assert.match(detail, /2026-09-19 11:00:00 UTC/);
  const security = await read('/packages/security');
  assert.equal((security.match(/Local patches, bundled dependencies and binary artifacts are not evaluated/g) || []).length, 1);
  assert.match(security, /0\.396%/);
  assert.match(security, /Reported fixes/);
  assert.match(security, /unavailable/);
  assert.match(security, />No<\/span>/);
  assert.match(security, /<section\b[^>]*id="security"[^>]*>/, 'evidence remains expanded');
  assert.equal((security.match(/>Observed version<\/dt>/g) || []).length, 1);
  assert.equal((security.match(/>Reported fixes<\/dt>/g) || []).length, 8);
  const versionList = await read('/?monitor=version');
  const versionViews = versionList.match(/<nav[^>]*aria-label="Version"[^]*?<\/nav>/)[0];
  assert.match(versionViews, /view=updates[^>]*>\s*<span class="choice-label"><span>Outdated<\/span><\/span><b>3<\/b>/);
  const updatesList = await read('/?monitor=version&view=updates');
  assert.match(updatesList, /view=updates[^>]*aria-current="page"[^>]*>\s*<span class="choice-label"><span>Outdated<\/span>/);
  assert.match(updatesList, /aria-label="Remove Outdated"/);
  assert.match(versionList, /class="choice-row-label">Related<\/span>/);
  assert.match(versionList, /<nav[^>]*aria-label="Related"/);
  const versionRelated = await read('/?monitor=version&signal=security&q=openssl&buildsystem=custom');
  const relatedNav = versionRelated.match(/<nav[^>]*aria-label="Related"[^]*?<\/nav>/)[0];
  assert.match(relatedNav, /signal=security[^>]*aria-current="page"/);
  for (const match of relatedNav.matchAll(/href="([^"]+)"/g)) {
    const link = new URL(match[1].replaceAll('&amp;', '&'), 'http://fixture');
    assert.equal(link.searchParams.get('monitor'), 'version');
    assert.equal(link.searchParams.get('q'), 'openssl');
    assert.equal(link.searchParams.get('buildsystem'), 'custom');
    assert.equal(link.searchParams.getAll('signal').length <= 1, true);
  }
  const versionTable = versionList.match(/<table\b[^]*?<\/table>/)[0];
  assert.match(versionTable, />Advisory 8<\/a>/);
  assert.match(versionTable, /signal=security/);
  assert.doesNotMatch(versionTable, /CVE-2026-|Reported fixes|Exploit probability/);
  const securityDetail = await read('/packages/security');
  assert.match(securityDetail, /href="#security"[^>]*>Advisory 8<\/a>/);
  assert.equal((securityDetail.match(/<section\b[^>]*id="security"/g) || []).length, 1);
  const securityList = await read('/?monitor=security');
  for (let i = 1; i <= 8; i++) assert.match(securityList, new RegExp('>CVE-2026-100' + i + '</a>'));
  const securityTable = securityList.match(/<table\b[^]*?<\/table>/)[0];
  assert.doesNotMatch(securityTable, /<details|Reported fixes|Observed version/);
  const requirementPage = await read('/packages/requires-upgrade');
  const requirementSection = requirementPage.match(/<section\b[^>]*id="requires"[^]*?<\/section>/)[0];
  assert.match(requirementSection, />RuntimeDeps<\/h2>/);
  for (const name of ['Python', 'libwidget', 'libexample']) assert.ok(requirementSection.includes(name));
  assert.match(requirementSection, /≥ 1.80<[^]*?>✓<[^]*?→[^]*?≥ 1.82<[^]*?>✗</);
  for (const mark of ['✓', '✗']) assert.ok(requirementSection.includes(`>${mark}<`));
  assert.match(requirementSection, /Observed dependency version does not satisfy the upstream declaration/);
  const optionalSection = requirementPage.match(/<section\b[^>]*id="requires-optional"[^]*?<\/section>/)[0];
  assert.match(optionalSection, /Optional library[^]*?condition not evaluated[^]*?>\?</);
  assert.doesNotMatch(optionalSection, /platform_python_implementation|sys_platform|speedups/);
  const conditions = requirementPage.match(/<details\b[^>]*id="requires-conditions"[^]*?<\/details>/)[0];
  assert.match(conditions, /platform_python_implementation[^]*?speedups/);
  assert.doesNotMatch(conditions.split('>')[0], /\bopen\b/);
  assert.doesNotMatch(requirementSection, /<details/);
  const requiresList = await read('/?monitor=requires');
  for (const name of ['Python', 'libwidget', 'Optional library', 'libexample']) assert.ok(requiresList.includes(name));
  assert.doesNotMatch(requiresList, /platform_python_implementation|sys_platform|speedups/);
  assert.match(requiresList, />RuntimeDeps<[^]*?>Optional RuntimeDeps</);
  const requiresRow = name => requiresList.match(new RegExp(`<tr data-key="${name}"[^]*?</tr>`))?.[0] ?? '';
  assert.match(requiresRow('requires-upgrade'), />2\.0<[^]*?→[^]*?>2\.1</);
  assert.match(requiresRow('requires-current'), /libcurrent/);
  assert.doesNotMatch(requiresRow('requires-current'), /→|Upgrade:|>2\.0</);
  assert.match(requiresRow('requires-target-only'), /libtarget<[^]*?Upgrade:[^]*?>✗</);
  const requirementNav = requiresList.match(/<nav[^>]*aria-label="Dependencies"[^]*?<\/nav>/)[0];
  for (const label of ['DepMismatch', 'DepChanges', 'Uncovered', 'CheckFailed']) assert.ok(requirementNav.includes(label));
  assert.match(requirementNav, /requires=unmet/);
  assert.match(requirementNav, /check=uncovered/);
  assert.match(requirementNav, /check=failed/);
  assert.match(requirementNav, /requires=changes/);
  const requiresFiltered = await read('/?monitor=requires&requires=unmet&build=rva23:blocked&per_page=1');
  assert.equal(lastQuery.get('requires'), 'unmet');
  assert.match(requiresFiltered, /name="requires" value="unmet"/);
  const filteredNav = requiresFiltered.match(/<nav[^>]*aria-label="Dependencies"[^]*?<\/nav>/)[0];
  for (const match of filteredNav.matchAll(/href="([^"]+)"/g)) {
    const link = new URL(match[1].replaceAll('&amp;', '&'), 'http://fixture');
    assert.equal(link.searchParams.get('page'), '1');
    assert.equal(link.searchParams.get('per_page'), '1');
    assert.deepEqual(link.searchParams.getAll('build'), []);
    if (link.searchParams.get('check')) assert.equal(link.searchParams.get('requires'), null);
  }
  const requiresCoverage = await read('/?monitor=requires&check=uncovered&requires=unmet&build=rva23:blocked');
  const coverageNav = requiresCoverage.match(/<nav[^>]*aria-label="Dependencies"[^]*?<\/nav>/)[0];
  for (const label of ['DepMismatch', 'DepChanges', 'Uncovered', 'CheckFailed']) assert.ok(coverageNav.includes(label));
  assert.doesNotMatch(requiresCoverage, /name="requires"/);
  assert.match(requiresCoverage, />Version<\/th>/);
  const uncovered = await read('/?monitor=version&check=uncovered');
  const uncoveredRow = uncovered.match(/<tr data-key="untracked"[^]*?<\/tr>/)[0];
  assert.match(uncoveredRow, />2\.0</);
  assert.doesNotMatch(uncoveredRow, /→/);
  const buildReason = await read('/packages/build-reason');
  assert.match(buildReason, /nothing provides &lt;fixture-dependency&gt;/);
  assert.doesNotMatch(buildReason, /worker:\/\/|nothing provides <fixture-dependency>/);
  const buildList = await read('/?monitor=build');
  assert.match(buildList, /status-matrix/);
  assert.equal((buildList.match(/class="choice-row"/g) || []).length, targets.length);
  assert.doesNotMatch(buildList.match(/<nav[^>]*aria-label="Views"[^]*?<\/nav>/)[0], /Stale/);
  for (const target of targets) {
    const controls = buildList.match(new RegExp(`<nav[^>]*aria-label="${target.label}"[^]*?</nav>`))[0];
    assert.match(controls, />Blocked<\/span><\/span>\s*<b>1<\/b>/);
  }
  const buildFiltered = await read('/?monitor=build&build=rva23:blocked&build=x86_64:blocked&q=openssl&buildsystem=custom');
  for (const target of targets) {
    const targetNav = buildFiltered.match(new RegExp(`<nav[^>]*aria-label="${target.label}"[^]*?</nav>`))[0];
    assert.equal((targetNav.match(/aria-current="page"/g) || []).length, target.id === 'rva20' ? 0 : 1, 'at most one selected status per target');
    for (const match of targetNav.matchAll(/href="([^"]+)"/g)) {
      const link = new URL(match[1].replaceAll('&amp;', '&'), 'http://fixture');
      assert.equal(link.searchParams.get('monitor'), 'build');
      assert.equal(link.searchParams.get('q'), 'openssl');
      assert.equal(link.searchParams.get('buildsystem'), 'custom');
      const builds = link.searchParams.getAll('build');
      assert.ok(builds.filter(value => value.startsWith(target.id + ':')).length <= 1);
      for (const retained of ['rva23:blocked', 'x86_64:blocked']) {
        if (!retained.startsWith(target.id + ':')) assert.ok(builds.includes(retained), 'other targets compose');
      }
    }
  }
  retainedCount = 2;
  const retainedBuilds = await read('/?monitor=build');
  const retainedNavigation = retainedBuilds.match(/<nav[^>]*aria-label="Views"[^]*?<\/nav>/)[0];
  assert.match(retainedNavigation, /freshness=retained[^]*?>Stale<\/span><\/span>\s*<b>2<\/b>/);
  retainedCount = 0;
  const selectedRetained = await read('/?monitor=build&freshness=retained');
  assert.match(selectedRetained.match(/<nav[^>]*aria-label="Views"[^]*?<\/nav>/)[0],
    /freshness=retained[^>]*aria-current="page"[^]*?>Stale<\/span><\/span>\s*<b>0<\/b>/);
  assert.match(buildList, /nothing provides &lt;fixture-dependency&gt;/);
  assert.doesNotMatch(buildList, /worker:\/\//);
  const aggregateReason = listing.match(/<tr class="row-note"[^]*?<\/tr>/)[0];
  assert.match(aggregateReason, /colspan="2" aria-hidden="true"/);
  assert.match(aggregateReason, /colspan="3"[^]*?rva23:[^]*?nothing provides &lt;fixture-dependency&gt;/);
  const aggregateRuntime = listing.match(/<tr data-key="requires-upgrade"[^]*?<\/tr>/)[0];
  assert.match(aggregateRuntime, /RuntimeDeps:[^]*?libwidget/);
  assert.doesNotMatch(aggregateRuntime, /Python|Optional library|platform_python_implementation/);
  assert.match(listing, /MIT → Apache-2.0/);
  assert.match(listing, /CVE-2026-1001/);

  assert.doesNotMatch(listing.match(/<table\b[^]*?<\/table>/)[0], /<details/);
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
  console.log('PASS documents: arbitrary monitor, opaque navigation, immediate GET, visible tables/fields, safe links, theme and retained evidence');
  // Wire-level tests deliberately bypass fetch's automatic decompression/cache.
  const identity = await wire('/');
  assert.equal(identity.status,200);assert.equal(identity.headers['content-encoding'],undefined);
  assert.equal(identity.headers['cache-control'],'private, no-cache');
  assert.match(identity.headers['content-security-policy'], /script-src 'self'/);
  assert.doesNotMatch(identity.headers['content-security-policy'], /unsafe-inline|unsafe-eval/);
  const detailResponse = await wire('/packages/success');
  assert.match(detailResponse.headers['content-security-policy'], /script-src 'self'/);
  assert.doesNotMatch(detailResponse.headers['content-security-policy'], /unsafe-inline|unsafe-eval/);
  const scripts = [...detailResponse.body.toString().matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)];
  assert.equal(scripts.length, 1);
  assert.match(scripts[0][1], /src="\/reveal-anchor\.js"/);
  assert.equal(scripts[0][2].trim(), '');
  const anchorScript = await wire('/reveal-anchor.js');
  assert.equal(anchorScript.status, 200);
  assert.match(anchorScript.headers['content-type'], /javascript/);
  assert.match((await wire('/livez')).headers['content-security-policy'], /script-src 'none'/);
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
  const appCSS = gunzipSync(css.body).toString();
  assert.match(appCSS, /\.value-tag\{[^}]*var\(--appearance-background/);
  assert.match(appCSS, /\.value-tag\[data-variant=solid\]\{[^}]*var\(--appearance-foreground/);
  assert.match(appCSS, /\[aria-current\]\[data-appearance\]\{[^}]*color-mix\([^}]*var\(--appearance-background/);
  assert.match(appCSS, /\[aria-current\]\[data-appearance\]\{[^}]*box-shadow:[^}]*var\(--appearance-background/);
  const range=await wire(cssPath,{'Accept-Encoding':'gzip',Range:'bytes=0-9'});assert.equal(range.status,206);assert.equal(range.headers['content-encoding'],undefined);assert.equal(range.body.length,10);
  assert.equal((await wire('/',{},'HEAD')).body.length,0);
  const json=await wire('/api/v2/packages',{'Accept-Encoding':'gzip'});assert.equal(json.status,200);assert.equal(json.headers['cache-control'],'no-store');assert.equal(json.headers['content-encoding'],'gzip');assert.ok(JSON.parse(gunzipSync(json.body)).items.length>0);
  const redirect=await wire('/theme?to=dark&from=%2F');assert.equal(redirect.status,303);assert.equal(redirect.headers['cache-control'],'no-store');assert.ok(redirect.headers['set-cookie']);assert.equal(redirect.headers.etag,undefined);
  for (const from of ['/\t/example.invalid', '/\n/example.invalid', '/\r/example.invalid',
      '//example.invalid', '/\\example.invalid', 'https://example.invalid']) {
    for (const to of ['dark', 'invalid']) {
      const response = await wire('/theme?' + new URLSearchParams({to, from}));
      assert.equal(response.status, 303);
      assert.equal(response.headers.location, '/', `Unsafe theme return: ${JSON.stringify(from)}`);
    }
  }
  const back = '/packages/a%2Bb?monitor=requires&q=one%20two#checks';
  const returned = await wire('/theme?' + new URLSearchParams({to: 'auto', from: back}));
  assert.equal(returned.status, 303);
  assert.equal(returned.headers.location, back);
  console.log('PASS theme redirect: control characters and external targets rejected; local path/query/fragment retained');
  unavailable=true;
  const failure=await wire('/');assert.equal(failure.status,503);assert.equal(failure.headers['cache-control'],'no-store');assert.equal(failure.headers.etag,undefined);
  unavailable=false;
  console.log('PASS transport: gzip/br, identity/q=0, decoded equality, ETag304, changed data/theme/query, immutable CSS GET, ranges, HEAD, JSON, errors, cookies and CSP');
  console.log('PASS SSR document renderer');
} finally {
  child.kill('SIGTERM'); await once(child, 'exit'); mock.closeAllConnections(); await new Promise(resolve => mock.close(resolve));
}
