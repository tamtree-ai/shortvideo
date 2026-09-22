// What a failed render says, reduced to a sentence.
//
// The runtime shows the author the *tail* of stderr, and Remotion folds a whole
// stack into `error.message` — so a clip that is not video used to reach the
// author as `…index.mjs:16260:18)`, which is the one part of the error nobody
// can act on (V3.5 acceptance). This keeps the first line that is not a stack
// frame, bounded, and nothing else.

const MAX = 300;

// A child process's own complaint beats the wrapper's. execa's message is
// "Command failed with exit code 1: <the whole ffprobe argv>"; the line that
// says what is wrong — "Invalid data found when processing input" — is the
// last line of the child's stderr, with its temp-file path in front of it.
const lastMeaningfulLine = (text) => {
  const last = String(text)
    .split('\n')
    .map((part) => part.trim())
    .filter((part) => part && !part.startsWith('at ') && !/^[{}]/.test(part))
    .pop();
  return last ? last.replace(/^\/\S+: /, '') : null;
};

const fromChildStderr = (error) => {
  // The child's stderr, wherever the wrapping left it: on the error, on its
  // cause, or — once Remotion has re-thrown it as a plain Error — folded into
  // the message after execa's "Command failed…" first line.
  for (const candidate of [error, error?.cause]) {
    if (typeof candidate?.stderr === 'string' && candidate.stderr.trim()) {
      return lastMeaningfulLine(candidate.stderr);
    }
  }
  const message = String(error?.message ?? '');
  if (message.startsWith('Command failed') && message.includes('\n')) {
    return lastMeaningfulLine(message.slice(message.indexOf('\n') + 1));
  }
  return null;
};

export const failureSentence = (error) => {
  const raw = String(fromChildStderr(error) ?? error?.message ?? error ?? 'unknown error');
  const line =
    raw
      .split('\n')
      .map((part) => part.trim())
      .find((part) => part && !part.startsWith('at ')) ?? 'unknown error';
  return line.length > MAX ? `${line.slice(0, MAX - 1)}…` : line;
};
