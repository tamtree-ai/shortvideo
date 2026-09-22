// The render document's TypeScript shape.
//
// The *authority* on these rules is `02-timeline-v1.md` and its enforcement is
// `tamtree_shortvideo/timeline.py`; this file only names the fields so the
// compositions can read them with the compiler's help. It deliberately does
// not restate a single bound — a second copy of "captions are at most 90
// characters" is a second thing to keep in step, and the one that drifts is
// always the copy furthest from the test.

export type Template = 'short-captioned' | 'short-plain';
export type ClipAudio = 'mute' | 'duck' | 'keep';

export type Caption = {
  text: string;
  start_frame: number;
  end_frame: number;
};

export type Clip = {
  file: string;
  mime_type: string;
  source_duration_seconds: number;
  in_seconds: number;
  out_seconds: number;
};

export type Transition = {
  kind: 'none' | 'crossfade';
  frames: number;
};

export type Beat = {
  index: number;
  start_frame: number;
  frames: number;
  pad_frames: number;
  clip: Clip;
  transition: Transition;
  captions: Caption[];
};

export type Narration = {
  file: string;
  mime_type: string;
  duration_seconds: number;
  target_lufs: number;
  true_peak_ceiling_dbtp: number;
};

export type Music = {
  file: string;
  mime_type: string;
  duration_seconds: number;
  target_lufs: number;
  duck_db: number;
  duck_attack_ms: number;
  duck_release_ms: number;
};

export type SafeArea = {
  inset_x: number;
  top: number;
  bottom: number;
};

export type RenderDocument = {
  version: 1;
  template: Template;
  width: number;
  height: number;
  fps: number;
  total_frames: number;
  clip_audio: ClipAudio;
  clip_duck_db: number;
  caption_safe_area: SafeArea;
  narration: Narration;
  music?: Music | null;
  beats: Beat[];
  digest?: string;
  /** Where the workdir's materialized media is served from this invocation —
   * added by the CLI, never by the node, because the port is not knowable
   * until the loopback server is listening. */
  base_url: string;
};

/** Decibels to a linear gain multiplier, which is the only volume unit a
 * browser has. `0 dB` is unity; the timeline's attenuations are negative. */
export const gainFromDb = (db: number): number => 10 ** (db / 20);

/** The frame the composition is on, translated to a media URL. */
export const mediaUrl = (document: RenderDocument, file: string): string =>
  `${document.base_url}/${file.split('/').map(encodeURIComponent).join('/')}`;
