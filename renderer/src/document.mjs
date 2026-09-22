// The render document, re-validated at the edge of the sandbox.
//
// `shortvideo.compose` already validated the *authoring* timeline before it
// materialized anything — this is not a second opinion on the same data. It is
// the check that the document which arrived is the document that was meant:
// self-consistent frame math, every media file present on disk, nothing
// outside the workdir. A renderer that trusted its input file would turn a
// malformed document into a plausible-looking wrong video, which is the one
// failure mode worse than an error.
//
// Deliberately dependency-free and deliberately not a schema library: the
// rules are few, and each error names the field so a failure reads as a
// defect report rather than a stack trace.

const TEMPLATES = new Set(['short-captioned', 'short-plain']);
const TRANSITIONS = new Set(['none', 'crossfade']);
const CLIP_AUDIO = new Set(['mute', 'duck', 'keep']);

export class DocumentError extends Error {}

const fail = (message) => {
  throw new DocumentError(message);
};

const number = (value, where) => {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    fail(`${where} must be a finite number, got ${JSON.stringify(value)}`);
  }
  return value;
};

const integer = (value, where) => {
  if (!Number.isInteger(value)) fail(`${where} must be an integer, got ${JSON.stringify(value)}`);
  return value;
};

const string = (value, where) => {
  if (typeof value !== 'string' || value === '') fail(`${where} must be a non-empty string`);
  return value;
};

/** A media path is workdir-relative and stays there. The sandbox already
 * confines the child, so this is not the only thing standing between a
 * traversal and the host — it is what turns one into a named error here
 * instead of an ENOENT three layers down inside a browser. */
const mediaPath = (value, where) => {
  const path = string(value, where);
  if (path.startsWith('/') || path.includes('..') || path.includes('\\')) {
    fail(`${where} must be a relative path inside the workdir, got ${JSON.stringify(path)}`);
  }
  return path;
};

/**
 * Validate a parsed render document and return it, with every media path
 * collected so the caller can assert each file exists before Chrome starts.
 */
export const parseRenderDocument = (raw) => {
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    fail('the render document must be a JSON object');
  }

  if (raw.version !== 1) fail(`unsupported timeline version ${JSON.stringify(raw.version)} (this renderer speaks 1)`);
  if (!TEMPLATES.has(raw.template)) {
    fail(`unknown template ${JSON.stringify(raw.template)} — expected one of ${[...TEMPLATES].join(', ')}`);
  }
  if (!CLIP_AUDIO.has(raw.clip_audio)) {
    fail(`clip_audio must be one of ${[...CLIP_AUDIO].join(', ')}, got ${JSON.stringify(raw.clip_audio)}`);
  }

  const width = integer(raw.width, 'width');
  const height = integer(raw.height, 'height');
  const fps = integer(raw.fps, 'fps');
  const totalFrames = integer(raw.total_frames, 'total_frames');
  if (width <= 0 || height <= 0 || fps <= 0 || totalFrames <= 0) {
    fail('width, height, fps and total_frames must all be positive');
  }

  const beats = raw.beats;
  if (!Array.isArray(beats) || beats.length === 0) fail('a timeline needs at least one beat');

  const paths = [];

  const narration = raw.narration;
  if (narration === null || typeof narration !== 'object') fail('narration is required');
  paths.push(mediaPath(narration.file, 'narration.file'));
  number(narration.duration_seconds, 'narration.duration_seconds');

  let music = null;
  if (raw.music !== undefined && raw.music !== null) {
    music = raw.music;
    paths.push(mediaPath(music.file, 'music.file'));
    // v1 does not loop a bed: a short one would leave silence under the tail,
    // and the timeline validator refuses that — so a document arriving here
    // with one is a document that did not come from the validator.
    if (number(music.duration_seconds, 'music.duration_seconds') + 1e-6 < totalFrames / fps) {
      fail('music is shorter than the video, and v1 does not loop a bed');
    }
  }

  // Boundaries are absolute and contiguous: beat i starts exactly where i-1
  // ended, and the last beat ends at total_frames. Re-derived rather than
  // trusted — a supplied value that disagrees is the bug this catches.
  let expectedStart = 0;
  beats.forEach((beat, index) => {
    const where = `beats[${index}]`;
    if (integer(beat.index, `${where}.index`) !== index) {
      fail(`${where}.index says ${beat.index} — beats must be in order and self-labelled`);
    }
    if (integer(beat.start_frame, `${where}.start_frame`) !== expectedStart) {
      fail(`${where}.start_frame is ${beat.start_frame}, but beat ${index - 1} ends at ${expectedStart}`);
    }
    const frames = integer(beat.frames, `${where}.frames`);
    if (frames <= 0) fail(`${where}.frames must be positive`);
    expectedStart += frames;

    const clip = beat.clip;
    if (clip === null || typeof clip !== 'object') fail(`${where}.clip is required`);
    paths.push(mediaPath(clip.file, `${where}.clip.file`));
    const inSeconds = number(clip.in_seconds, `${where}.clip.in_seconds`);
    const outSeconds = number(clip.out_seconds, `${where}.clip.out_seconds`);
    const sourceSeconds = number(clip.source_duration_seconds, `${where}.clip.source_duration_seconds`);
    if (inSeconds < 0 || outSeconds <= inSeconds) fail(`${where}.clip trim is inverted or empty`);
    if (outSeconds > sourceSeconds + 1e-6) {
      fail(`${where}.clip is trimmed past the end of its source (${outSeconds}s of ${sourceSeconds}s)`);
    }

    const transition = beat.transition ?? {kind: 'none', frames: 0};
    if (!TRANSITIONS.has(transition.kind)) {
      fail(`${where}.transition.kind ${JSON.stringify(transition.kind)} is not supported`);
    }
    const transitionFrames = integer(transition.frames ?? 0, `${where}.transition.frames`);
    if (transition.kind === 'none' && transitionFrames !== 0) {
      fail(`${where}.transition is "none" but asks for ${transitionFrames} frames`);
    }
    if (index === 0 && transition.kind !== 'none') {
      fail('beat 0 must have no transition — there is nothing to dissolve from');
    }
    // A crossfade is paid for out of the OUTGOING clip's kept tail. Checked
    // here as well as in the node because it is the one timeline rule whose
    // violation produces a visibly wrong render rather than an error.
    if (transition.kind === 'crossfade') {
      const previous = beats[index - 1].clip;
      const tail = previous.source_duration_seconds - previous.out_seconds;
      if (tail + 1e-6 < transitionFrames / fps) {
        fail(`${where}.transition needs ${transitionFrames} frames of tail from beat ${index - 1}, which has ${tail.toFixed(3)}s`);
      }
    }

    const captions = beat.captions ?? [];
    if (!Array.isArray(captions)) fail(`${where}.captions must be a list`);
    if (raw.template === 'short-captioned' && captions.length === 0) {
      fail(`${where} has no caption, and short-captioned requires one on every beat`);
    }
    if (raw.template === 'short-plain' && captions.length > 0) {
      fail(`${where} carries captions, and short-plain forbids them`);
    }
    captions.forEach((caption, captionIndex) => {
      const at = `${where}.captions[${captionIndex}]`;
      string(caption.text, `${at}.text`);
      const start = integer(caption.start_frame, `${at}.start_frame`);
      const end = integer(caption.end_frame, `${at}.end_frame`);
      if (end <= start) fail(`${at} ends before it starts`);
      if (start < beat.start_frame || end > beat.start_frame + frames) {
        fail(`${at} runs outside its own beat`);
      }
    });
  });

  if (expectedStart !== totalFrames) {
    fail(`beats sum to ${expectedStart} frames but total_frames says ${totalFrames}`);
  }

  return {document: raw, mediaPaths: paths};
};
