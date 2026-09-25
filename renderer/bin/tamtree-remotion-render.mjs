#!/usr/bin/env node
// The executable `ComposePreset` names as argv[0]. Thin on purpose: argument
// handling, rendering and validation are importable modules so they can be
// tested without a browser, and this file only decides what an error looks
// like from the outside — a sentence on stderr and a non-zero exit, never a
// stack trace the curated runtime would hand a workflow author verbatim.

import {UsageError} from '../src/args.mjs';
import {DocumentError} from '../src/document.mjs';
import {render} from '../src/render.mjs';
import {failureSentence} from '../src/report.mjs';

try {
  const result = await render(process.argv.slice(2));
  process.stdout.write(`${JSON.stringify(result)}\n`);
} catch (error) {
  const known = error instanceof UsageError || error instanceof DocumentError;
  process.stderr.write(`${known ? error.message : `render failed: ${failureSentence(error)}`}\n`);
  process.exit(1);
}
