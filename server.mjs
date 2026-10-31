import http from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.dirname(fileURLToPath(import.meta.url));
const port = Number(process.env.PORT || 4175);
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.jpg': 'image/jpeg', '.png': 'image/png', '.svg': 'image/svg+xml', '.json': 'application/json', '.webmanifest': 'application/manifest+json' };
// Development preview only. Deploy just the static assets, never this source directory.
const allowed = new Set(['index.html', 'app.js', 'styles.css', 'store.js', 'cloud-store.js', 'manifest.webmanifest', 'favicon.svg']);
http.createServer(async (req, res) => {
  try {
    const name = decodeURIComponent(new URL(req.url, 'http://localhost').pathname).replace(/^\/+/, '') || 'index.html';
    if (!allowed.has(name) && !/^assets\/[a-zA-Z0-9_.-]+$/.test(name)) { res.writeHead(404); res.end('Not found'); return; }
    const target = path.resolve(root, name);
    if (!target.startsWith(root + path.sep) || !(await stat(target)).isFile()) throw new Error('Missing');
    res.writeHead(200, { 'Content-Type': types[path.extname(target)] || 'application/octet-stream', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer', 'Cache-Control': 'no-store', 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self' https:; frame-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'" });
    res.end(await readFile(target));
  } catch { res.writeHead(404); res.end('Not found'); }
}).listen(port, '127.0.0.1', () => process.stdout.write(`Local: http://127.0.0.1:${port}\n`));
