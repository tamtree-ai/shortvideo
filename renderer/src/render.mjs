// `tamtree-remotion-render` — render one TimelineV1 render document to an mp4.
//
// The whole executable is this file plus a document validator and a loopback
// media server. It takes no free-form input: every argument is a path or a
// bounded value chosen by `ComposePreset`, and the only *content* it reads is
// the render document, which rides a file rather than argv precisely so that
// caption text can never become a command-line token (D1).
//
// Three properties it is written to keep, each of which the V0.3 sandbox spike
// showed is easy to lose:
//
//  1. **It never downloads anything.** Remotion resolves its cached browser
//     relative to the CWD, and SEC-D1 hands the child a fresh empty workdir —
//     so without an absolute `--browser` it fetches 88 MB of Chrome on *every*
//     invocation and executes it. This refuses to start unless it is given an
//     absolute browser path that exists.
//  2. **It fails before Chrome rather than after.** The document is validated
//     and every media file stat'd first; a render that was going to fail for a
//     missing clip fails in milliseconds, not after a browser has started.
//  3. **It reports honestly.** One JSON object on stdout, and a non-zero exit
//     with a plain sentence on stderr. No partial output file is left behind.

import {stat, rm} from 'node:fs/promises';
import {readFileSync} from 'node:fs';
import {isAbsolute, join, resolve} from 'node:path';

import {renderMedia, selectComposition} from '@remotion/renderer';

import {UsageError, parseArgs} from './args.mjs';
import {DocumentError, parseRenderDocument} from './document.mjs';
import {serveMedia} from './media-server.mjs';

const mustBeAnExistingDirectoryOrFile = async (path, what, kind) => {
  if (!isAbsolute(path)) {
    throw new UsageError(
      `${what} must be an absolute path — Remotion resolves relative paths against the ` +
        `per-invocation workdir, where nothing from the image is visible (and where a missing ` +
        `browser means a silent 88 MB download on every render)`,
    );
  }
  let info;
  try {
    info = await stat(path);
  } catch {
    throw new UsageError(`${what} does not exist on this worker: ${path}`);
  }
  if (kind === 'dir' && !info.isDirectory()) throw new UsageError(`${what} is not a directory: ${path}`);
  if (kind === 'file' && !info.isFile()) throw new UsageError(`${what} is not a file: ${path}`);
};

export const render = async (argv, {cwd = process.cwd()} = {}) => {
  const args = parseArgs(argv);

  // The image's half of the contract, checked before anything else: an
  // absolute pre-built bundle and an absolute browser that both exist here.
  await mustBeAnExistingDirectoryOrFile(args.bundle, '--bundle', 'dir');
  await mustBeAnExistingDirectoryOrFile(args.browser, '--browser', 'file');

  const timelinePath = resolve(cwd, args.timeline);
  let parsed;
  try {
    parsed = parseRenderDocument(JSON.parse(readFileSync(timelinePath, 'utf8')));
  } catch (error) {
    if (error instanceof DocumentError) throw error;
    throw new DocumentError(`the render document could not be read: ${error.message}`);
  }
  const {document, mediaPaths} = parsed;

  // Every input present before a browser exists (property 2 above).
  for (const path of mediaPaths) {
    const full = join(cwd, path);
    try {
      const info = await stat(full);
      if (!info.isFile()) throw new Error('not a file');
    } catch {
      throw new DocumentError(`the render document names ${path}, which was not materialized`);
    }
  }

  const started = Date.now();
  const media = await serveMedia(cwd);
  const outputLocation = resolve(cwd, args.out);

  try {
    // The composition reads its media over loopback; nothing else about the
    // document changes between validation and render. It is nested under
    // `document` because that is the single prop the composition declares —
    // a spread would make every field of the timeline its own prop, and a
    // renamed field would then arrive as `undefined` instead of as an error.
    const inputProps = {document: {...document, base_url: media.baseUrl}};

    const composition = await selectComposition({
      serveUrl: args.bundle,
      id: document.template,
      inputProps,
      browserExecutable: args.browser,
      logLevel: 'error',
    });
    const selected = Date.now();

    await renderMedia({
      composition,
      serveUrl: args.bundle,
      codec: 'h264',
      audioCodec: 'aac',
      outputLocation,
      inputProps,
      concurrency: args.concurrency,
      browserExecutable: args.browser,
      // swiftshader: the worker has no GPU, and letting Chrome fall back on
      // its own picks a different rasteriser depending on the host — which
      // would make "the same timeline renders deterministically" untrue.
      chromiumOptions: {gl: 'swiftshader'},
      scale: args.scale,
      logLevel: 'error',
    });

    const info = await stat(outputLocation);
    return {
      ok: true,
      template: document.template,
      digest: document.digest ?? null,
      width: Math.round(composition.width * args.scale),
      height: Math.round(composition.height * args.scale),
      fps: composition.fps,
      frames: composition.durationInFrames,
      scale: args.scale,
      bytes: info.size,
      select_ms: selected - started,
      render_ms: Date.now() - selected,
      total_ms: Date.now() - started,
    };
  } catch (error) {
    // A half-written mp4 is worse than none: the runtime collects whatever is
    // at the output path, and a truncated file would be saved as a render.
    await rm(outputLocation, {force: true});
    throw error;
  } finally {
    await media.close();
  }
};
