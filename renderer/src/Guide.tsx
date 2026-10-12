import React from 'react';
import {AbsoluteFill, Audio, Easing, Sequence, interpolate, useCurrentFrame} from 'remotion';

import {Captions} from './Captions';
import {BeatClip, FadingOut} from './Short';
import {gainFromDb, mediaUrl, type Beat, type CameraKey, type GuideOverlay, type RenderDocument, type TrackPoint} from './timeline';

// `guide-16x9`: a screen-recorded user guide. Each beat is one section of the
// guide, playing that section's recording, and carries the overlay
// `tamtree_shortvideo.guide` derived from the capture log. Everything that
// moves here is data in the document — the camera's keys, the cursor's track,
// the clicks, the callouts — and this file only draws it. The timing rules of
// the short (a transition never moves a boundary, a short clip holds its last
// frame, loudness is decided upstream) are the same, and so is the code: the
// clip and the dissolve are Short's own `BeatClip` and `FadingOut`.
//
// Two layers per beat, and the split is the point:
//
//  * **The camera layer** holds the recording, scaled and translated so the
//    camera's centre sits mid-frame. The recording is 2x, so up to the zoom
//    ceiling this magnifies real pixels rather than inventing them.
//  * **The pointer layer** is drawn in screen space *over* the camera, with
//    every position mapped through the same camera. A cursor or a callout
//    stroke drawn inside the camera layer would grow with the zoom — a cursor
//    the size of a thumb at 2x — so they are placed by the camera and sized by
//    the frame.

const ACCENT = '#FFB020';
const INK = '#14161C';
const FONT = '"Tamtree Caption", "Inter", "Helvetica Neue", Arial, sans-serif';
const ease = Easing.inOut(Easing.cubic);

type View = {x: number; y: number; zoom: number};
const WIDE: View = {x: 0.5, y: 0.5, zoom: 1};

/** The camera at an absolute frame: eased between the keys either side,
 * holding the first before it and the last after it. */
export const cameraAt = (keys: CameraKey[], frame: number): View => {
  if (keys.length === 0) return WIDE;
  if (frame <= keys[0].frame) return keys[0];
  const last = keys[keys.length - 1];
  if (frame >= last.frame) return last;
  let index = 0;
  while (keys[index + 1].frame < frame) index += 1;
  const from = keys[index];
  const to = keys[index + 1];
  const k = ease((frame - from.frame) / (to.frame - from.frame));
  return {
    x: from.x + (to.x - from.x) * k,
    y: from.y + (to.y - from.y) * k,
    zoom: from.zoom + (to.zoom - from.zoom) * k,
  };
};

/** The cursor at an absolute frame, linear between recorded points — the
 * track is already the eased path the capture drew, one point per frame. */
export const cursorAt = (track: TrackPoint[], frame: number): {x: number; y: number} | null => {
  if (track.length === 0) return null;
  if (frame <= track[0][0]) return {x: track[0][1], y: track[0][2]};
  const last = track[track.length - 1];
  if (frame >= last[0]) return {x: last[1], y: last[2]};
  let index = 0;
  while (track[index + 1][0] < frame) index += 1;
  const [f0, x0, y0] = track[index];
  const [f1, x1, y1] = track[index + 1];
  const k = (frame - f0) / (f1 - f0);
  return {x: x0 + (x1 - x0) * k, y: y0 + (y1 - y0) * k};
};

/** A recording position (fractions) to a frame pixel, through the camera. */
const toScreen = (view: View, x: number, y: number, width: number, height: number) => ({
  left: ((x - view.x) * view.zoom + 0.5) * width,
  top: ((y - view.y) * view.zoom + 0.5) * height,
});

const Cursor: React.FC<{left: number; top: number; size: number}> = ({left, top, size}) => (
  <svg
    width={size}
    height={size * 1.5}
    viewBox="0 0 16 24"
    style={{position: 'absolute', left, top, overflow: 'visible', filter: 'drop-shadow(0 2px 3px rgba(0,0,0,0.45))'}}
  >
    {/* The tip is at (0, 0), which is what `left`/`top` place. */}
    <path d="M0 0 L0 17 L4.2 13.2 L7 19.8 L9.8 18.6 L7.1 12.2 L12.6 12.2 Z" fill="white" stroke="black" strokeWidth="1.2" strokeLinejoin="round" />
  </svg>
);

const PointerLayer: React.FC<{document: RenderDocument; beat: Beat; overlay: GuideOverlay}> = ({
  document,
  beat,
  overlay,
}) => {
  const local = useCurrentFrame();
  const frame = beat.start_frame + local;
  const {width, height, fps} = document;
  const view = cameraAt(overlay.camera, frame);
  const unit = height / 1080;

  const callouts = overlay.callouts
    .filter((callout) => frame >= callout.start_frame && frame < callout.end_frame)
    .map((callout, index) => {
      // Strictly increasing for Remotion: the hold between the fades is at
      // least one frame, and a callout too short to fade simply shows.
      const fade = Math.min(6, Math.floor((callout.end_frame - callout.start_frame - 1) / 2));
      const opacity =
        fade > 0
          ? interpolate(
              frame,
              [callout.start_frame, callout.start_frame + fade, callout.end_frame - fade, callout.end_frame],
              [0, 1, 1, 0],
              {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'},
            )
          : 1;
      const a = toScreen(view, callout.box.x, callout.box.y, width, height);
      const b = toScreen(view, callout.box.x + callout.box.w, callout.box.y + callout.box.h, width, height);
      const pad = 8 * unit;
      const box = {left: a.left - pad, top: a.top - pad, width: b.left - a.left + 2 * pad, height: b.top - a.top + 2 * pad};
      // The label sits above the box unless that would leave the frame.
      const labelAbove = box.top > 70 * unit;
      const centre = Math.min(Math.max(box.left + box.width / 2, 160 * unit), width - 160 * unit);
      return (
        <div key={`callout-${index}`} style={{position: 'absolute', inset: 0, opacity}}>
          <div
            style={{
              position: 'absolute',
              ...box,
              border: `${4 * unit}px solid ${ACCENT}`,
              borderRadius: 10 * unit,
              boxShadow: `0 0 0 ${3 * unit}px rgba(20,22,28,0.35)`,
            }}
          />
          <div
            style={{
              position: 'absolute',
              left: centre,
              top: labelAbove ? box.top - 14 * unit : box.top + box.height + 14 * unit,
              transform: `translate(-50%, ${labelAbove ? '-100%' : '0'})`,
              maxWidth: width * 0.4,
              padding: `${8 * unit}px ${16 * unit}px`,
              borderRadius: 999,
              backgroundColor: ACCENT,
              color: INK,
              fontFamily: FONT,
              fontWeight: 700,
              fontSize: 30 * unit,
              lineHeight: 1.2,
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              boxShadow: '0 4px 14px rgba(0,0,0,0.35)',
            }}
          >
            {callout.text}
          </div>
        </div>
      );
    });

  const ripples = overlay.clicks
    .filter(([at]) => frame >= at && frame < at + Math.round(fps * 0.45))
    .map(([at, x, y]) => {
      const k = (frame - at) / (fps * 0.45);
      const {left, top} = toScreen(view, x, y, width, height);
      const radius = (10 + 34 * k) * unit;
      return (
        <div
          key={`click-${at}`}
          style={{
            position: 'absolute',
            left: left - radius,
            top: top - radius,
            width: radius * 2,
            height: radius * 2,
            borderRadius: '50%',
            border: `${4 * unit}px solid ${ACCENT}`,
            opacity: 0.85 * (1 - k),
          }}
        />
      );
    });

  const pointer = cursorAt(overlay.cursor, frame);
  const cursor = pointer ? toScreen(view, pointer.x, pointer.y, width, height) : null;

  return (
    <AbsoluteFill>
      {callouts}
      {ripples}
      {cursor ? <Cursor left={cursor.left} top={cursor.top} size={22 * unit * (1 + (view.zoom - 1) * 0.35)} /> : null}
    </AbsoluteFill>
  );
};

const CameraLayer: React.FC<{document: RenderDocument; beat: Beat; children: React.ReactNode}> = ({
  document,
  beat,
  children,
}) => {
  const local = useCurrentFrame();
  const view = cameraAt(beat.guide?.camera ?? [], beat.start_frame + local);
  const {width, height} = document;
  return (
    <AbsoluteFill
      style={{
        transformOrigin: '0 0',
        transform: `translate(${(0.5 - view.x * view.zoom) * width}px, ${(0.5 - view.y * view.zoom) * height}px) scale(${view.zoom})`,
      }}
    >
      {children}
    </AbsoluteFill>
  );
};

const TitleCard: React.FC<{text: string; frames: number; height: number; fadeIn: boolean}> = ({
  text,
  frames,
  height,
  fadeIn,
}) => {
  const frame = useCurrentFrame();
  // Remotion wants a strictly increasing range, so a fade that is not there
  // is left out rather than given zero length — which a 1-frame card needs too.
  const out = Math.min(10, Math.floor(frames / 3));
  const into = fadeIn ? Math.min(8, Math.floor(frames / 3)) : 0;
  const range: number[] = [];
  const values: number[] = [];
  if (into > 0) range.push(0), values.push(0);
  range.push(into), values.push(1);
  if (frames - out > into) range.push(frames - out), values.push(1);
  if (out > 0) range.push(frames), values.push(0);
  const opacity =
    range.length > 1 ? interpolate(frame, range, values, {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'}) : 1;
  const unit = height / 1080;
  return (
    <AbsoluteFill
      style={{
        opacity,
        backgroundColor: 'rgba(12, 14, 20, 0.82)',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      <div style={{width: 72 * unit, height: 6 * unit, borderRadius: 3 * unit, backgroundColor: ACCENT, marginBottom: 28 * unit}} />
      <div
        style={{
          maxWidth: '70%',
          color: 'white',
          fontFamily: FONT,
          fontWeight: 700,
          fontSize: 64 * unit,
          lineHeight: 1.15,
          textAlign: 'center',
          textWrap: 'balance',
        }}
      >
        {text}
      </div>
    </AbsoluteFill>
  );
};

export const Guide: React.FC<{document: RenderDocument}> = ({document}) => {
  const {beats, width, height} = document;

  return (
    <AbsoluteFill style={{backgroundColor: 'black'}}>
      {beats.map((beat, index) => {
        const next = beats[index + 1];
        const borrowed = next && next.transition.kind === 'crossfade' ? next.transition.frames : 0;
        const playFrames = beat.frames - beat.pad_frames;
        return (
          <Sequence
            key={beat.index}
            from={beat.start_frame}
            durationInFrames={beat.frames + borrowed}
            // Earlier beats above later ones — see Short.tsx for why.
            style={{zIndex: beats.length - index}}
          >
            <FadingOut frames={beat.frames} borrowed={borrowed}>
              <CameraLayer document={document} beat={beat}>
                <BeatClip document={document} beat={beat} playFrames={playFrames} borrowedFrames={borrowed} />
              </CameraLayer>
              {beat.guide ? <PointerLayer document={document} beat={beat} overlay={beat.guide} /> : null}
            </FadingOut>
          </Sequence>
        );
      })}

      {/* Captions and title cards sit above all footage, as in Short.tsx —
          captions on top, because a section's first line is spoken while its
          title card is still up, and a caption under the card reads dimmed. */}
      {beats.map((beat) =>
        beat.captions.length > 0 ? (
          <Sequence key={`captions-${beat.index}`} from={beat.start_frame} durationInFrames={beat.frames} style={{zIndex: beats.length + 2}}>
            <Captions
              captions={beat.captions.map((caption) => ({
                ...caption,
                start_frame: caption.start_frame - beat.start_frame,
                end_frame: caption.end_frame - beat.start_frame,
              }))}
              safeArea={document.caption_safe_area}
              width={width}
              height={height}
              variant="guide"
            />
          </Sequence>
        ) : null,
      )}
      {beats.map((beat) =>
        beat.guide?.title ? (
          <Sequence
            key={`title-${beat.index}`}
            from={beat.start_frame}
            durationInFrames={beat.guide.title.frames}
            style={{zIndex: beats.length + 1}}
          >
            {/* The opening card is up from the first frame; later ones fade
                in over the cut so they do not flash. */}
            <TitleCard text={beat.guide.title.text} frames={beat.guide.title.frames} height={height} fadeIn={beat.index > 0} />
          </Sequence>
        ) : null,
      )}

      <Audio src={mediaUrl(document, document.narration.file)} />
      {document.music ? (
        <Audio src={mediaUrl(document, document.music.file)} volume={gainFromDb(document.music.duck_db)} />
      ) : null}
    </AbsoluteFill>
  );
};
