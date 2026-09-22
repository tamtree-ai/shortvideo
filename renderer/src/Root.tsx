import React from 'react';
import {Composition} from 'remotion';

import {Short} from './Short';
import type {RenderDocument} from './timeline';

// Two compositions, one component. The ids are the template names the timeline
// carries, so `selectComposition({id: document.template})` needs no mapping
// table — and an unknown template fails in Remotion's own "no composition with
// that id" rather than silently rendering the other one.
//
// Every dimension comes from `calculateMetadata`, i.e. from the document: the
// frozen v1 values are 1080×1920 @ 30fps, but they are *fields*, so a future
// square or 60fps variant needs no new composition. The draft render at half
// size is the same composition with `scale`, never a second definition —
// a draft that composed differently would make the approval meaningless.

const calculateMetadata = ({props}: {props: {document: RenderDocument}}) => ({
  durationInFrames: props.document.total_frames,
  fps: props.document.fps,
  width: props.document.width,
  height: props.document.height,
});

// Remotion needs *something* to render the editor preview with before a real
// document arrives. An empty timeline is not valid, so the placeholder is one
// frame of black rather than a fake short — the CLI always passes a real
// document through `inputProps`.
const placeholder = {
  version: 1,
  template: 'short-plain',
  width: 1080,
  height: 1920,
  fps: 30,
  total_frames: 1,
  clip_audio: 'mute',
  clip_duck_db: -18,
  caption_safe_area: {inset_x: 0.08, top: 0.66, bottom: 0.84},
  narration: {file: '', mime_type: 'audio/wav', duration_seconds: 0, target_lufs: -16, true_peak_ceiling_dbtp: -1.5},
  beats: [],
  base_url: '',
} as unknown as RenderDocument;

export const RemotionRoot: React.FC = () => (
  <>
    <Composition
      id="short-captioned"
      component={Short}
      calculateMetadata={calculateMetadata}
      defaultProps={{document: placeholder}}
      durationInFrames={1}
      fps={30}
      width={1080}
      height={1920}
    />
    <Composition
      id="short-plain"
      component={Short}
      calculateMetadata={calculateMetadata}
      defaultProps={{document: placeholder}}
      durationInFrames={1}
      fps={30}
      width={1080}
      height={1920}
    />
  </>
);
