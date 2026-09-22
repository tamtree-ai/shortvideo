// Fetch Chrome Headless Shell once, at image build time, and move it to a
// fixed absolute path. Never run per render: the render child is network-off,
// and a renderer that could download a browser would download one *and execute
// it* (V0.3 finding 4).
//
// The version is not chosen here — it is the one `@remotion/renderer` pins
// (`TESTED_VERSION`), so the browser moves exactly when the lockfile does.
// What this adds is the record: the pinning package and the sha256 of the
// executable land in `browser.json` beside it, which is what the SBOM step and
// a reviewer read.
import {createHash} from 'node:crypto';
import {cpSync, readdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync} from 'node:fs';
import {basename, dirname, join, resolve} from 'node:path';

import {ensureBrowser} from '@remotion/renderer';

const target = resolve(process.argv[2] ?? 'chrome');

await ensureBrowser({chromeMode: 'headless-shell', logLevel: 'error'});

// Remotion caches under node_modules/.remotion/chrome-headless-shell/<platform>/,
// and the executable's name depends on the platform: `chrome-headless-shell`
// on linux64 (Google's build), `headless_shell` on linux-arm64 (Playwright's
// build, which Remotion uses there). Both land at one arch-independent path so
// `RemotionBackend`'s default is true on every architecture.
const EXECUTABLE_NAMES = ['chrome-headless-shell', 'headless_shell'];
const cache = resolve('node_modules/.remotion/chrome-headless-shell');
const find = (dir) => {
  for (const entry of readdirSync(dir, {withFileTypes: true})) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      const hit = find(full);
      if (hit) return hit;
    } else if (EXECUTABLE_NAMES.includes(entry.name)) {
      return full;
    }
  }
  return null;
};
const executable = find(cache);
if (!executable) {
  process.stderr.write(`no chrome-headless-shell executable under ${cache}\n`);
  process.exit(1);
}

rmSync(target, {recursive: true, force: true});
cpSync(dirname(executable), target, {recursive: true});
rmSync(cache, {recursive: true, force: true});

const moved = join(target, 'chrome-headless-shell');
const original = join(target, basename(executable));
if (original !== moved) renameSync(original, moved);
const record = {
  name: 'chrome-headless-shell',
  // The version string is recorded by the image's runtime stage, which runs
  // the executable (and so also proves its shared libraries are present).
  pinned_by: `@remotion/renderer@${JSON.parse(readFileSync('node_modules/@remotion/renderer/package.json', 'utf8')).version}`,
  executable: moved,
  upstream_name: basename(executable),
  sha256: createHash('sha256').update(readFileSync(moved)).digest('hex'),
  bytes: statSync(moved).size,
};
writeFileSync(join(target, 'browser.json'), JSON.stringify(record, null, 2) + '\n');
process.stdout.write(`${JSON.stringify(record)}\n`);
