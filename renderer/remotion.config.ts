// Bundler-side configuration only. Everything about *one render* — codec,
// concurrency, scale, the browser path — is an argument the curated preset
// compiles, never a config file: a render must be fully determined by its
// argv and its document, so that the same timeline renders the same way on
// any worker that has this image.
import {Config} from '@remotion/cli/config';

Config.setVideoImageFormat('jpeg');
Config.setOverwriteOutput(true);
