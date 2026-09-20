// Keep Astro's routing/static-file handler; add only the HTTP transport boundary.
// SPDX-License-Identifier: MulanPSL-2.0
import {createServer as httpServer} from 'node:http';
import {createServer as httpsServer} from 'node:https';
import {readFileSync} from 'node:fs';
import {constants} from 'node:zlib';
import compression from 'compression';

process.env.ASTRO_NODE_AUTOSTART = 'disabled';
const {handler} = await import('./dist/server/entry.mjs');
const compress = compression({
  threshold: 1024,
  brotli: {params: {[constants.BROTLI_PARAM_QUALITY]: 4}},
  // A byte range describes the identity representation, not a compressed one.
  filter: (req, res) => !req.headers.range && res.statusCode !== 206 && compression.filter(req, res),
});
const serve = (req, res) => compress(req, res, error => {
  if (error) { res.writeHead(500); res.end(); return; }
  handler(req, res);
});
const {SERVER_CERT_PATH: cert, SERVER_KEY_PATH: key} = process.env;
const server = cert && key
  ? httpsServer({cert: readFileSync(cert), key: readFileSync(key)}, serve)
  : httpServer(serve);
server.listen(Number(process.env.PORT || 8080), process.env.HOST || '0.0.0.0');
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => server.close());
