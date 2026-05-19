// Voice intent classification — turns transcribed speech into one of:
//   "affirmative" (yes / go ahead / please / sure / continue)
//   "negative"    (no / not now / cancel / stop / skip / later)
//   "stop"        (hard stop / cancel current action / never mind)
//   "wake"        (the agent is being addressed: "Jarvis, ...")
//   "speech"      (ordinary speech with no special intent — pass through to chat)
//
// Used by:
//   - ElaborationBanner — accept / dismiss the spoken elaboration prompt
//   - Confirmation gates (when PR 28 lands) — fire-or-cancel the suspended tool
//   - Always-on listener — decide whether speech is a command vs background chatter
//
// Heuristics are deliberately conservative — false positives are worse than
// false negatives because they trigger irreversible actions (sending email,
// creating workflows, etc.). When in doubt, treat as "speech" not "affirmative".

export type VoiceIntent =
  | 'affirmative'
  | 'negative'
  | 'stop'
  | 'wake'
  | 'speech';

const AFFIRMATIVE_PATTERNS = [
  // Direct affirmatives
  /\byes\b/,
  /\byeah\b/,
  /\byep\b/,
  /\byup\b/,
  /\bsure\b/,
  /\bok(ay)?\b/,
  // Polite go-aheads (Jarvis context)
  /\bplease\b/,
  /\bgo ahead\b/,
  /\bgo on\b/,
  /\bcarry on\b/,
  /\bproceed\b/,
  /\bcontinue\b/,
  /\bof course\b/,
  /\bcertainly\b/,
  /\babsolutely\b/,
  // Action verbs that implicitly accept the proposed elaboration / send
  /\belaborat[e]\b/,
  /\bexpand\b/,
  /\btell me more\b/,
  /\bdo it\b/,
  /\bsend it\b/,
  /\bfire (it|away)\b/,
];

const NEGATIVE_PATTERNS = [
  /\bno\b(?!\s+thank)/,  // "no" but NOT "no thank you" (handled separately as polite no)
  /\bnope\b/,
  /\bnot now\b/,
  /\bnot yet\b/,
  /\bskip\b/,
  /\bdismiss\b/,
  /\blater\b/,
  /\bnah\b/,
  /\bno thanks?\b/,
  /\bno thank you\b/,
];

const STOP_PATTERNS = [
  /\bstop\b/,
  /\bcancel\b/,
  /\bnever mind\b/,
  /\bnevermind\b/,
  /\babort\b/,
  /\bforget it\b/,
  /\bquiet\b/,
  /\bshut up\b/,
];

const WAKE_PATTERNS = [
  /\bjarvis\b/i,
  /\bhey jarvis\b/i,
  /\bok jarvis\b/i,
  /\bok\.? jarvis\b/i,
  /\bwake up\b/i,   // bare hands-free activation phrase
  /\bcomputer\b/,  // Star Trek mode
];

/**
 * Classify a piece of transcribed speech. Caller decides what to do based
 * on the current UI context (is there a pending elaboration? confirmation?).
 *
 * Whitespace is normalised and case is lowered before matching.
 */
export function classifyIntent(transcript: string): VoiceIntent {
  if (!transcript) return 'speech';
  const text = transcript.trim().toLowerCase().replace(/\s+/g, ' ');

  // Stop wins — if the user says "cancel" we never want to misinterpret it.
  if (STOP_PATTERNS.some((p) => p.test(text))) return 'stop';

  // Wake word check (only counts if it's a clear address, not buried).
  if (WAKE_PATTERNS.some((p) => p.test(text))) return 'wake';

  // Negative beats affirmative — "no" is a hard signal even if the rest
  // of the phrase contains "please" ("no please don't").
  if (NEGATIVE_PATTERNS.some((p) => p.test(text))) return 'negative';

  if (AFFIRMATIVE_PATTERNS.some((p) => p.test(text))) return 'affirmative';

  return 'speech';
}

/**
 * Extract the part of the transcript AFTER the wake word, if any.
 * "Hey Jarvis, what's on my calendar?" -> "what's on my calendar?"
 * "Jarvis"                              -> "" (just the wake word, no command)
 */
export function stripWakeWord(transcript: string): string {
  const text = transcript.trim();
  const stripped = text.replace(
    /^(hey|ok|okay)\s+jarvis[,\s]*/i,
    '',
  ).replace(
    /^jarvis[,\s]*/i,
    '',
  );
  return stripped.trim();
}

// Verbal "send" trigger phrases — radio-comms style. When the user ends
// a command with one of these, we auto-submit the buffered input
// regardless of hands-free mode. Cheap & explicit; works in noisy
// environments where silence-detection can't fire.
const CLOSE_PHRASES = [
  /\b(over)\s*$/i,
  /\b(send it)\s*$/i,
  /\b(send that)\s*$/i,
  /\b(do it)\s*$/i,
  /\b(go)\s*$/i,
  /\b(fire it)\s*$/i,
  /\b(go ahead)\s*$/i,
  /\b(make it so)\s*$/i,  // Star Trek bonus
];

/** True when the transcript ends with an explicit submission cue. */
export function endsWithSendTrigger(transcript: string): boolean {
  const text = transcript.trim().toLowerCase();
  return CLOSE_PHRASES.some((p) => p.test(text));
}

/** Strip the trailing close phrase so the actual command is clean. */
export function stripCloseTrigger(transcript: string): string {
  let text = transcript.trim();
  for (const p of CLOSE_PHRASES) {
    text = text.replace(p, '');
  }
  // Tidy up trailing punctuation/whitespace.
  return text.replace(/[\s,.;:!?]+$/, '').trim();
}

// Abort phrases — said within ~1s before an auto-submit fires, they
// cancel the pending submission. "Wait" / "cancel" / "hold on" / "scratch that".
const ABORT_PATTERNS = [
  /\bwait\b/,
  /\bhold on\b/,
  /\bscratch that\b/,
  /\bnever mind\b/,
  /\bnevermind\b/,
  /\bcancel\b/,
  /\bactually\s+no\b/,
];

/** True when speech is asking us to back out of an in-flight command. */
export function isAbort(transcript: string): boolean {
  const text = transcript.trim().toLowerCase();
  return ABORT_PATTERNS.some((p) => p.test(text));
}
