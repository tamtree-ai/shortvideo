import React from 'react';
import {AbsoluteFill, useCurrentFrame} from 'remotion';

import type {Caption, SafeArea} from './timeline';

// The caption band is a *template* constant, not a timeline field: the
// timeline says what the caption is and when, never where. It arrives in the
// render document so that one document fully determines one render — but it is
// the same three numbers on every v1 short (8% inset, 66%–84% of height).
//
// Below 84% is where every short-form surface puts its own chrome — handle,
// description, progress bar, CTA — and above 66% is the frame's subject.

export const Captions: React.FC<{
  captions: Caption[];
  safeArea: SafeArea;
  width: number;
  height: number;
}> = ({captions, safeArea, width, height}) => {
  const frame = useCurrentFrame();
  // Captions within a beat are strictly ordered and non-overlapping, so at
  // most one is live. `find` rather than `filter` states that.
  const current = captions.find((caption) => frame >= caption.start_frame && frame < caption.end_frame);
  if (!current) return null;

  const inset = Math.round(width * safeArea.inset_x);
  const top = Math.round(height * safeArea.top);
  const bandHeight = Math.round(height * (safeArea.bottom - safeArea.top));

  return (
    <AbsoluteFill>
      <div
        style={{
          position: 'absolute',
          left: inset,
          right: inset,
          top,
          height: bandHeight,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        <span
          style={{
            // Sized off the frame rather than in fixed pixels, so the draft
            // render at 540×960 is the same composition at half scale rather
            // than a different-looking one.
            fontSize: Math.round(width * 0.062),
            lineHeight: 1.25,
            // The stack's first entry is what V3.4 bakes into the image with
            // redistribution rights; the rest are what a dev machine has.
            fontFamily: '"Tamtree Caption", "Inter", "Helvetica Neue", Arial, sans-serif',
            fontWeight: 700,
            color: 'white',
            textAlign: 'center',
            textWrap: 'balance',
            // Legibility over arbitrary footage, without a plate that would
            // cover the frame: a tight shadow reads on light and dark alike.
            textShadow: '0 2px 12px rgba(0,0,0,0.55), 0 0 2px rgba(0,0,0,0.9)',
            // Whitespace is preserved because the timeline permits a single
            // newline and means it as a line break.
            whiteSpace: 'pre-wrap',
          }}
        >
          {current.text}
        </span>
      </div>
    </AbsoluteFill>
  );
};
