// A loopback file server for the workdir's materialized media.
//
// **Why this exists at all.** Remotion serves the compiled bundle to Chrome
// over `http://127.0.0.1`, and a page served over http cannot load `file://`
// resources — Chrome blocks it. The bundle's own `public/` directory is baked
// into the image at build time, so it cannot hold per-invocation media either.
// So the clips and the narration have to be reachable over http, and the only
// http the sandbox allows is the one inside its own network namespace. That is
// exactly what SEC-D1's loopback-up change buys (contracts 1.36.0): an empty
// netns with `lo` up, where this server is reachable by this render and by
// nothing else in the world.
//
// It is as small as a server can be: GET only, one directory, no listing, no
// symlink following, bound to 127.0.0.1 on an ephemeral port.

import {createReadStream} from 'node:fs';
import {stat, realpath} from 'node:fs/promises';
import {createServer} from 'node:http';
import {join, resolve, sep} from 'node:path';

const MIME = {
  '.mp4': 'video/mp4',
  '.webm': 'video/webm',
  '.wav': 'audio/wav',
  '.mp3': 'audio/mpeg',
  '.ogg': 'audio/ogg',
  '.m4a': 'audio/mp4',
};

const contentType = (path) => {
  const dot = path.lastIndexOf('.');
  return (dot === -1 ? null : MIME[path.slice(dot).toLowerCase()]) ?? 'application/octet-stream';
};

/**
 * Serve `root` on 127.0.0.1 and return `{baseUrl, close}`.
 *
 * Range requests are honoured because Remotion's frame extraction seeks rather
 * than reading a clip end to end; without it every seek would re-read the
 * whole file.
 */
export const serveMedia = async (root) => {
  const base = await realpath(resolve(root));

  const server = createServer((request, response) => {
    const deny = (code) => {
      response.writeHead(code);
      response.end();
    };
    if (request.method !== 'GET' && request.method !== 'HEAD') return deny(405);

    // `new URL` decodes the path and normalizes `..` before we ever touch the
    // filesystem; the realpath check below is what actually holds the line.
    let relative;
    try {
      relative = decodeURIComponent(new URL(request.url, 'http://127.0.0.1').pathname).replace(/^\/+/, '');
    } catch {
      return deny(400);
    }

    const target = join(base, relative);
    realpath(target)
      .then(async (real) => {
        if (real !== base && !real.startsWith(base + sep)) return deny(403);
        const info = await stat(real);
        if (!info.isFile()) return deny(404);

        const range = request.headers.range;
        const match = range ? /^bytes=(\d*)-(\d*)$/.exec(range) : null;
        const headers = {
          'content-type': contentType(real),
          'accept-ranges': 'bytes',
          'cache-control': 'no-store',
        };

        if (!match) {
          response.writeHead(200, {...headers, 'content-length': info.size});
          if (request.method === 'HEAD') return response.end();
          return createReadStream(real).pipe(response);
        }

        const start = match[1] === '' ? Math.max(0, info.size - Number(match[2])) : Number(match[1]);
        const end = match[1] === '' || match[2] === '' ? info.size - 1 : Math.min(Number(match[2]), info.size - 1);
        if (!Number.isFinite(start) || start > end) {
          response.writeHead(416, {'content-range': `bytes */${info.size}`});
          return response.end();
        }
        response.writeHead(206, {
          ...headers,
          'content-length': end - start + 1,
          'content-range': `bytes ${start}-${end}/${info.size}`,
        });
        if (request.method === 'HEAD') return response.end();
        return createReadStream(real, {start, end}).pipe(response);
      })
      .catch(() => deny(404));
  });

  await new Promise((done, failed) => {
    server.once('error', failed);
    server.listen(0, '127.0.0.1', done);
  });

  const {port} = server.address();
  return {
    baseUrl: `http://127.0.0.1:${port}`,
    close: () =>
      new Promise((done) => {
        server.closeAllConnections?.();
        server.close(() => done());
      }),
  };
};
