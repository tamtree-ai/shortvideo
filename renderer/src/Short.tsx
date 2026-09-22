import React from 'react';
import {AbsoluteFill, Audio, Freeze, OffthreadVideo, Sequence, interpolate, useCurrentFrame} from 'remotion';

import {Captions} from './Captions';
import {gainFromDb, mediaUrl, type Beat, type RenderDocument} from './timeline';

// One composition renders both templates. They differ in exactly one thing —
// whether captions are drawn — and the timeline validator has already refused
// a `short-plain` that carries captions or a `short-captioned` that is missing
// one, so this component does not re-decide it: it draws what the document
// holds.
//
// Three rules from `02-timeline-v1.md` are load-bearing here and are worth
// stating where they are implemented rather than only where they are written:
//
//  * **A transition never moves a beat boundary.** A crossfade into beat i is
//    drawn by extending the *outgoing* clip past its out point — into the tail
//    the trim deliberately kept — and dissolving it over the first N frames of
//    beat i. The audio underneath never moves, which is why a draft render and
//    a final render of the same timeline cut in the same places.
//  * **A short clip holds its last frame** for at most `pad_frames`. The
//    timeline rejects anything longer, so a pad here is always sub-half-second.
//  * **Loudness is not decided here.** The document's `target_lufs` describes
//    normalization that has already been applied to the file upstream; a
//    browser cannot measure integrated loudness, and a renderer that pretended
//    to would produce a number nobody could trust. What this does honour is
//    every *relative* level: the clip-audio policy and the music duck.

/** The clip for one beat: trimmed at the head, holding its last frame for the
 * pad, and continuing past its own beat for however long the next beat's
 * crossfade borrows from it. */
const BeatClip: React.FC<{
  document: RenderDocument;
  beat: Beat;
  playFrames: number;
  borrowedFrames: number;
}> = ({document, beat, playFrames, borrowedFrames}) => {
  const frame = useCurrentFrame();
  const {fps, clip_audio: policy, clip_duck_db: duckDb} = document;

  // `startFrom` is the head trim; the tail is not bounded here on purpose —
  // the frames past `out_seconds` are exactly what a following crossfade
  // spends, and the enclosing Sequence is what stops the clip.
  const video = (
    <OffthreadVideo
      src={mediaUrl(document, beat.clip.file)}
      startFrom={Math.round(beat.clip.in_seconds * fps)}
      muted={policy === 'mute'}
      volume={policy === 'duck' ? gainFromDb(duckDb) : 1}
      style={{width: '100%', height: '100%', objectFit: 'cover'}}
    />
  );

  // Past the material, hold the last frame we actually have rather than
  // cutting to black. `playFrames` counts the frames the source can cover;
  // anything after it is the pad, and anything after *that* is borrowed tail,
  // which by construction the source does have.
  const held = playFrames + borrowedFrames;
  if (beat.pad_frames > 0 && frame >= playFrames) {
    return <Freeze frame={Math.max(0, Math.min(frame, playFrames - 1))}>{video}</Freeze>;
  }
  return frame < held ? video : null;
};

export const Short: React.FC<{document: RenderDocument}> = ({document}) => {
  const {beats, fps, width, height} = document;

  return (
    <AbsoluteFill style={{backgroundColor: 'black'}}>
      {beats.map((beat, index) => {
        // What the *next* beat's crossfade takes out of this one's tail.
        const next = beats[index + 1];
        const borrowed = next && next.transition.kind === 'crossfade' ? next.transition.frames : 0;
        // The frames this beat's source can actually cover: its own length
        // minus whatever is padded by holding the last frame.
        const playFrames = beat.frames - beat.pad_frames;

        return (
          <Sequence
            key={beat.index}
            from={beat.start_frame}
            durationInFrames={beat.frames + borrowed}
            // Earlier beats sit *above* later ones, so a dissolve is the
            // outgoing clip fading away to reveal the incoming one already
            // playing underneath. Getting this backwards renders a fade from
            // the wrong shot — and looks almost right, which is worse.
            style={{zIndex: beats.length - index}}
            layout="none"
          >
            <FadingOut frames={beat.frames} borrowed={borrowed}>
              <BeatClip
                document={document}
                beat={beat}
                playFrames={playFrames}
                borrowedFrames={borrowed}
              />
              {beat.captions.length > 0 ? (
                <Captions
                  captions={beat.captions.map((caption) => ({
                    ...caption,
                    // Caption frames are absolute in the document; inside this
                    // Sequence the clock restarts at the beat.
                    start_frame: caption.start_frame - beat.start_frame,
                    end_frame: caption.end_frame - beat.start_frame,
                  }))}
                  safeArea={document.caption_safe_area}
                  width={width}
                  height={height}
                />
              ) : null}
            </FadingOut>
          </Sequence>
        );
      })}

      <Audio src={mediaUrl(document, document.narration.file)} />

      {document.music ? (
        // v1 does not sidechain: the narration runs the length of the video, so
        // "ducked whenever narration plays" and "ducked throughout" are the
        // same mix. Attack and release ride in the document for the version
        // that does measure, and are deliberately unused here rather than
        // approximated into something that only looks like ducking.
        <Audio
          src={mediaUrl(document, document.music.file)}
          volume={gainFromDb(document.music.duck_db)}
        />
      ) : null}
    </AbsoluteFill>
  );
};

/** Holds children at full opacity for `frames`, then dissolves them out across
 * the `borrowed` frames the next beat's crossfade spends. */
const FadingOut: React.FC<{frames: number; borrowed: number; children: React.ReactNode}> = ({
  frames,
  borrowed,
  children,
}) => {
  const frame = useCurrentFrame();
  if (borrowed === 0) return <AbsoluteFill>{children}</AbsoluteFill>;
  const opacity = interpolate(frame, [frames, frames + borrowed], [1, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  return <AbsoluteFill style={{opacity}}>{children}</AbsoluteFill>;
};
