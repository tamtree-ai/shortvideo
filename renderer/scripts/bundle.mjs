// Build the static site the renderer serves to Chrome. Run once at image
// build time (V3.4), never per render: bundling per invocation would put a
// compiler in the sandbox and make every render depend on node_modules being
// present there.
import {bundle} from '@remotion/bundler';
import {resolve} from 'node:path';

const outDir = process.argv[2] ?? resolve(process.cwd(), 'build');
const serveUrl = await bundle({
  entryPoint: resolve(process.cwd(), 'src/index.ts'),
  outDir,
  publicDir: null,
  onProgress: () => {},
});
process.stdout.write(`${serveUrl}\n`);
