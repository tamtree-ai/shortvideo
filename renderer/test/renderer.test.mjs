// The renderer's own tests, on `node:test` with no dependencies at all.
//
// They cover the three pieces that decide whether a render is the right one,
// and every one of them runs before Chrome exists — which is exactly why they
// can run here, in a checkout with no `node_modules`, rather than only inside
// the image V3.4 builds. What they deliberately do NOT cover is the render
// itself: a composition's output is a video, and asserting on one needs the
// image, a browser and a golden frame. That is V3.4/V3.5's job.
//
//   node --test renderer/test

import assert from 'node:assert/strict';
import {mkdtemp, mkdir, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {after, describe, test} from 'node:test';

import {UsageError, parseArgs} from '../src/args.mjs';
import {DocumentError, parseRenderDocument} from '../src/document.mjs';
import {serveMedia} from '../src/media-server.mjs';

const ARGV = [
  '--timeline', '_in/0.json',
  '--out', 'output.mp4',
  '--bundle', '/opt/bundle',
  '--browser', '/opt/chrome',
];

const beat = (index, start, frames, {captions = true, transition = null} = {}) => ({
  index,
  start_frame: start,
  frames,
  pad_frames: 0,
  clip: {
    file: `_in/${index + 2}.mp4`,
    mime_type: 'video/mp4',
    source_duration_seconds: frames / 30 + 1,
    in_seconds: 0,
    out_seconds: frames / 30,
  },
  transition: transition ?? {kind: 'none', frames: 0},
  captions: captions ? [{text: `Beat ${index}.`, start_frame: start, end_frame: start + frames}] : [],
});

const document = (overrides = {}) => ({
  version: 1,
  template: 'short-captioned',
  width: 1080,
  height: 1920,
  fps: 30,
  total_frames: 120,
  clip_audio: 'mute',
  clip_duck_db: -18,
  caption_safe_area: {inset_x: 0.08, top: 0.66, bottom: 0.84},
  narration: {
    file: '_in/1.wav',
    mime_type: 'audio/wav',
    duration_seconds: 4,
    target_lufs: -16,
    true_peak_ceiling_dbtp: -1.5,
  },
  beats: [beat(0, 0, 60), beat(1, 60, 60)],
  ...overrides,
});

describe('parseArgs', () => {
  test('accepts the argv ComposePreset compiles', () => {
    const args = parseArgs(ARGV);
    assert.equal(args.timeline, '_in/0.json');
    assert.equal(args.scale, 1);
    assert.equal(args.concurrency, 1);
  });

  test('refuses an argument it does not understand rather than ignoring it', () => {
    // The failure this prevents: a preset and an executable drift apart, and
    // the render quietly happens with the old meaning of a flag.
    assert.throws(() => parseArgs([...ARGV, '--no-sandbox', 'true']), UsageError);
  });

  test('requires the four arguments a render cannot be performed without', () => {
    for (const flag of ['--timeline', '--out', '--bundle', '--browser']) {
      const index = ARGV.indexOf(flag);
      const without = [...ARGV.slice(0, index), ...ARGV.slice(index + 2)];
      assert.throws(() => parseArgs(without), UsageError, `${flag} should be required`);
    }
  });

  test('bounds scale and concurrency', () => {
    assert.throws(() => parseArgs([...ARGV, '--scale', '2']), UsageError);
    assert.throws(() => parseArgs([...ARGV, '--scale', 'half']), UsageError);
    assert.throws(() => parseArgs([...ARGV, '--concurrency', '0']), UsageError);
    assert.throws(() => parseArgs([...ARGV, '--concurrency', '99']), UsageError);
    assert.equal(parseArgs([...ARGV, '--scale', '0.5']).scale, 0.5);
  });

  test('refuses a repeated flag instead of picking one', () => {
    assert.throws(() => parseArgs([...ARGV, '--scale', '0.5', '--scale', '1']), UsageError);
  });
});

describe('parseRenderDocument', () => {
  test('accepts a well-formed document and collects every media path', () => {
    const {mediaPaths} = parseRenderDocument(document());
    assert.deepEqual(mediaPaths, ['_in/1.wav', '_in/2.mp4', '_in/3.mp4']);
  });

  test('refuses a version it does not speak', () => {
    assert.throws(() => parseRenderDocument(document({version: 2})), DocumentError);
  });

  test('re-derives the frame math rather than trusting it', () => {
    // A supplied value that disagrees with the beats is a bug upstream, and a
    // renderer that took it on faith would produce a plausible wrong video.
    assert.throws(() => parseRenderDocument(document({total_frames: 121})), DocumentError);
    const gap = document();
    gap.beats[1].start_frame = 61;
    assert.throws(() => parseRenderDocument(gap), DocumentError);
  });

  test('refuses a media path that tries to leave the workdir', () => {
    const escaping = document();
    escaping.narration.file = '../../etc/passwd';
    assert.throws(() => parseRenderDocument(escaping), DocumentError);
    const absolute = document();
    absolute.narration.file = '/etc/passwd';
    assert.throws(() => parseRenderDocument(absolute), DocumentError);
  });

  test('refuses a crossfade the outgoing clip cannot pay for', () => {
    // The tail the trim kept is what a dissolve spends; a transition longer
    // than that tail is a wrong render rather than an error, unless caught.
    const broke = document();
    broke.beats[0].clip.source_duration_seconds = broke.beats[0].clip.out_seconds;
    broke.beats[1].transition = {kind: 'crossfade', frames: 8};
    assert.throws(() => parseRenderDocument(broke), DocumentError);

    const fine = document();
    fine.beats[1].transition = {kind: 'crossfade', frames: 8};
    assert.doesNotThrow(() => parseRenderDocument(fine));
  });

  test('beat 0 can have nothing to dissolve from', () => {
    const first = document();
    first.beats[0].transition = {kind: 'crossfade', frames: 4};
    assert.throws(() => parseRenderDocument(first), DocumentError);
  });

  test('holds the two templates apart', () => {
    const plainWithCaptions = document({template: 'short-plain'});
    assert.throws(() => parseRenderDocument(plainWithCaptions), DocumentError);

    const captionedWithout = document();
    captionedWithout.beats[1].captions = [];
    assert.throws(() => parseRenderDocument(captionedWithout), DocumentError);

    const plain = document({template: 'short-plain', beats: [beat(0, 0, 60, {captions: false}), beat(1, 60, 60, {captions: false})]});
    assert.doesNotThrow(() => parseRenderDocument(plain));
  });

  test('refuses a caption that runs outside its own beat', () => {
    const spilling = document();
    spilling.beats[0].captions[0].end_frame = 90;
    assert.throws(() => parseRenderDocument(spilling), DocumentError);
  });

  test('refuses a clip trimmed past the end of its source', () => {
    const over = document();
    over.beats[0].clip.out_seconds = over.beats[0].clip.source_duration_seconds + 1;
    assert.throws(() => parseRenderDocument(over), DocumentError);
  });

  test('refuses music shorter than the video, because v1 does not loop a bed', () => {
    const short = document({
      music: {file: '_in/9.mp3', mime_type: 'audio/mpeg', duration_seconds: 1, target_lufs: -20, duck_db: -12, duck_attack_ms: 150, duck_release_ms: 400},
    });
    assert.throws(() => parseRenderDocument(short), DocumentError);
  });
});

describe('serveMedia', () => {
  const dirs = [];
  after(async () => {
    await Promise.all(dirs.map((dir) => rm(dir, {recursive: true, force: true})));
  });

  const workdir = async () => {
    const dir = await mkdtemp(join(tmpdir(), 'shortvideo-'));
    dirs.push(dir);
    await mkdir(join(dir, '_in'), {recursive: true});
    await writeFile(join(dir, '_in', '1.wav'), 'abcdefghij');
    return dir;
  };

  test('serves a materialized file over loopback', async () => {
    const dir = await workdir();
    const media = await serveMedia(dir);
    try {
      assert.match(media.baseUrl, /^http:\/\/127\.0\.0\.1:\d+$/);
      const response = await fetch(`${media.baseUrl}/_in/1.wav`);
      assert.equal(response.status, 200);
      assert.equal(await response.text(), 'abcdefghij');
    } finally {
      await media.close();
    }
  });

  test('honours range requests, because frame extraction seeks', async () => {
    const dir = await workdir();
    const media = await serveMedia(dir);
    try {
      const response = await fetch(`${media.baseUrl}/_in/1.wav`, {headers: {range: 'bytes=2-4'}});
      assert.equal(response.status, 206);
      assert.equal(response.headers.get('content-range'), 'bytes 2-4/10');
      assert.equal(await response.text(), 'cde');
    } finally {
      await media.close();
    }
  });

  test('serves nothing outside the workdir', async () => {
    const dir = await workdir();
    const media = await serveMedia(dir);
    try {
      for (const path of ['/../../../etc/passwd', '/%2e%2e/%2e%2e/etc/passwd', '/_in/../../etc/passwd']) {
        const response = await fetch(`${media.baseUrl}${path}`);
        assert.ok(response.status >= 400, `${path} should not be served (got ${response.status})`);
      }
    } finally {
      await media.close();
    }
  });

  test('refuses anything but a read', async () => {
    const dir = await workdir();
    const media = await serveMedia(dir);
    try {
      const response = await fetch(`${media.baseUrl}/_in/1.wav`, {method: 'PUT', body: 'x'});
      assert.equal(response.status, 405);
    } finally {
      await media.close();
    }
  });
});

describe('failureSentence', () => {
  test('keeps the first line that is not a stack frame', async () => {
    const {failureSentence} = await import('../src/report.mjs');
    const error = new Error(
      'Error decoding frame: invalid data found\n    at Compositor.run (index.mjs:16260:18)\n    at next',
    );
    assert.equal(failureSentence(error), 'Error decoding frame: invalid data found');
  });

  test('skips a message that opens with a frame', async () => {
    const {failureSentence} = await import('../src/report.mjs');
    assert.equal(failureSentence({message: '  at x (y.mjs:1:1)\nEFBIG: file too large'}), 'EFBIG: file too large');
  });

  test("prefers the child process's own complaint over the wrapper's", async () => {
    const {failureSentence} = await import('../src/report.mjs');
    const error = Object.assign(new Error('Command failed with exit code 1: /opt/x/ffprobe -v error /tmp/a.mp4'), {
      stderr: '[mov,mp4 @ 0x1] moov atom not found\n/tmp/remotion-assets/4946.mp4: Invalid data found when processing input',
    });
    assert.equal(failureSentence(error), 'Invalid data found when processing input');
  });

  test('finds the complaint once Remotion has re-thrown it as a plain Error', async () => {
    const {failureSentence} = await import('../src/report.mjs');
    const error = new Error(
      'Command failed with exit code 1: /opt/x/ffprobe -v error /tmp/a.mp4\n' +
        '[mov,mp4 @ 0x1] moov atom not found\n' +
        '/tmp/remotion-assets/4946.mp4: Invalid data found when processing input\n' +
        '    at makeError (error.js:60:11)',
    );
    assert.equal(failureSentence(error), 'Invalid data found when processing input');
  });

  test('bounds a runaway line', async () => {
    const {failureSentence} = await import('../src/report.mjs');
    assert.equal(failureSentence(new Error('x'.repeat(1000))).length, 300);
  });
});
