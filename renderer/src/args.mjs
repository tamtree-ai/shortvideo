// Argument parsing, in its own module so it can be exercised without a
// browser, a bundle or 200 MB of node_modules — `render.mjs` imports
// `@remotion/renderer` at load, and a test that had to install Remotion to
// check that `--scale 2` is refused would simply never be run.
//
// Strict on purpose: every flag known, every flag at most once, nothing
// positional. A renderer that ignored an argument it did not understand would
// render the wrong thing the day a preset and an executable drift apart, and
// it would do it silently.

export class UsageError extends Error {}

const FLAGS = new Set(['--timeline', '--out', '--bundle', '--browser', '--scale', '--concurrency']);
const REQUIRED = ['--timeline', '--out', '--bundle', '--browser'];

export const parseArgs = (argv) => {
  const given = new Map();
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    if (!FLAGS.has(flag)) throw new UsageError(`unknown argument ${JSON.stringify(flag)}`);
    if (given.has(flag)) throw new UsageError(`${flag} was given twice`);
    const value = argv[index + 1];
    if (value === undefined) throw new UsageError(`${flag} needs a value`);
    given.set(flag, value);
  }

  for (const required of REQUIRED) {
    if (!given.has(required)) throw new UsageError(`${required} is required`);
  }

  const scale = given.has('--scale') ? Number(given.get('--scale')) : 1;
  if (!Number.isFinite(scale) || scale <= 0 || scale > 1) {
    throw new UsageError('--scale must be greater than 0 and at most 1');
  }
  const concurrency = given.has('--concurrency') ? Number(given.get('--concurrency')) : 1;
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 16) {
    throw new UsageError('--concurrency must be an integer between 1 and 16');
  }

  return {
    timeline: given.get('--timeline'),
    out: given.get('--out'),
    bundle: given.get('--bundle'),
    browser: given.get('--browser'),
    scale,
    concurrency,
  };
};
