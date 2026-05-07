// Tiny wrapper around the browser's SpeechSynthesis API.
// Buffers streamed tokens and speaks on sentence boundaries.
//
// Public API:
//   speak(text)              - speak a single utterance now
//   onTokenStream(delta)     - feed a streamed token; speak when buffer
//                              hits a sentence boundary
//   flush()                  - speak any remaining buffered text
//   cancel()                 - stop speaking and clear queue
//   isSupported()            - true if window.speechSynthesis exists

const SENTENCE_END = /[.!?\n]\s*$/;
// Don't speak if buffer is too short — wait for more tokens.
const MIN_FLUSH_LEN = 12;

let buffer = '';

export function isSupported(): boolean {
  return typeof window !== 'undefined' && 'speechSynthesis' in window;
}

// Score a voice by how close it gets to a "Jarvis" vibe — calm British
// male, ideally a higher-quality "Natural"/"Neural"/"Online" model the
// OS installed. Higher score = preferred. The chat UI uses
// window.speechSynthesis (browser-native), so the available voices
// depend on the user's OS + browser; we just bias selection toward the
// best one available.
function scoreVoice(v: SpeechSynthesisVoice): number {
  const name = v.name.toLowerCase();
  const lang = v.lang.toLowerCase();
  let score = 0;

  // Heavy bias toward British English.
  if (lang.startsWith('en-gb')) score += 100;
  else if (lang.startsWith('en')) score += 30;

  // Modern high-quality TTS engines mark their voices with these tags.
  // Edge ships "Microsoft <Name> Online (Natural)" which is markedly
  // better than the legacy SAPI voices.
  if (/(natural|neural|online|premium|enhanced)/i.test(name)) score += 50;

  // British male names commonly shipped on Windows / Edge / macOS.
  // "Mark", "Daniel", "Ryan", "George", "Harry", "James", "Oliver",
  // "Thomas" — all calm masculine English voices.
  if (
    /\b(mark|daniel|ryan|george|harry|james|oliver|thomas|arthur|tony)\b/i.test(
      name,
    )
  ) {
    score += 25;
  }

  // Prefer local voices when quality is otherwise tied (no network
  // round-trip, more privacy).
  if (v.localService) score += 5;

  return score;
}

function pickVoice(): SpeechSynthesisVoice | null {
  if (!isSupported()) return null;
  const voices = window.speechSynthesis.getVoices();
  if (!voices.length) return null;

  // Sort by Jarvis-affinity score (descending). Highest scorer wins.
  const ranked = [...voices].sort((a, b) => scoreVoice(b) - scoreVoice(a));
  const best = ranked[0];
  if (best && scoreVoice(best) > 0) return best;

  // Defensive fallback: original "any voice in user's language" logic.
  const lang = (navigator.language || 'en-US').toLowerCase();
  const langPrefix = lang.split('-')[0];
  const local = voices.find(
    (v) => v.localService && v.lang.toLowerCase().startsWith(langPrefix),
  );
  if (local) return local;
  const anyLang = voices.find((v) =>
    v.lang.toLowerCase().startsWith(langPrefix),
  );
  if (anyLang) return anyLang;
  return voices[0] ?? null;
}

function enqueue(text: string): void {
  if (!isSupported() || !text.trim()) return;
  const u = new SpeechSynthesisUtterance(text);
  const voice = pickVoice();
  if (voice) u.voice = voice;
  u.rate = 1.0;
  u.pitch = 1.0;
  u.volume = 1.0;
  window.speechSynthesis.speak(u);
}

export function speak(text: string): void {
  if (!isSupported()) return;
  enqueue(text);
}

export function onTokenStream(delta: string): void {
  if (!isSupported() || !delta) return;
  buffer += delta;
  // Flush whenever we cross a sentence boundary AND have enough characters.
  if (SENTENCE_END.test(buffer) && buffer.trim().length >= MIN_FLUSH_LEN) {
    const toSpeak = buffer;
    buffer = '';
    enqueue(toSpeak);
  }
}

export function flush(): void {
  if (!isSupported()) return;
  if (buffer.trim()) {
    const toSpeak = buffer;
    buffer = '';
    enqueue(toSpeak);
  }
}

export function cancel(): void {
  if (!isSupported()) return;
  buffer = '';
  window.speechSynthesis.cancel();
}

export function isSpeaking(): boolean {
  return isSupported() && window.speechSynthesis.speaking;
}
