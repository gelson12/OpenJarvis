// Wire protocol for the JARVIS on-screen widget system.
//
// The LiveKit worker publishes JSON commands on the `jarvis-ui` data
// topic; the browser listens and drives a widget store. Keep this file
// dependency-free — it is the shared contract between worker and UI.

export const JARVIS_UI_TOPIC = 'jarvis-ui';

/** Data topic for the live remote-browser widget (frames + interactions). */
export const JARVIS_BROWSER_TOPIC = 'jarvis-browser';

/** Every kind of floating panel JARVIS can place on screen. */
export type WidgetKind =
  | 'clock'
  | 'chat'
  | 'music'
  | 'search'
  | 'news'
  | 'youtube'
  | 'maps'
  | 'browser'
  | 'apps'
  | 'system'
  | 'cti';

/** A live widget panel rendered on screen. */
export interface WidgetInstance {
  id: string;
  kind: WidgetKind;
  title: string;
  payload?: unknown;
  x: number;
  y: number;
  w: number;
  h: number;
  z: number;
}

/** Props every widget component receives from the registry. */
export interface WidgetComponentProps {
  widget: WidgetInstance;
}

// ── Widget payload shapes (worker → widget) ──────────────────────────

/** A single web-search result — item in the `search` widget payload. */
export interface SearchResult {
  title: string;
  url: string;
  snippet: string;
}
export interface SearchPayload {
  query: string;
  results: SearchResult[];
}

/** A single headline — item in the `news` widget payload. */
export interface NewsArticle {
  title: string;
  url: string;
  source: string;
  published: string;
}
export interface NewsPayload {
  query: string;
  articles: NewsArticle[];
}

/** A single video — item in the `youtube` widget payload. */
export interface YouTubeVideo {
  videoId: string;
  title: string;
  channel: string;
  thumbnail: string;
}
export interface YouTubePayload {
  query: string;
  videos: YouTubeVideo[];
}

/** `maps` widget payload — a place, address, or directions query. */
export interface MapsPayload {
  query: string;
}

/** `browser` widget payload — frames stream separately over JARVIS_BROWSER_TOPIC. */
export interface BrowserPayload {
  loading?: boolean;
}

/** `cti` widget payload — an embedded OpenCTI dashboard/page. The worker
 * may supply a full `url` (overriding the env-baked default) and an
 * optional `path` to deep-link a specific dashboard within OpenCTI. */
export interface CTIPayload {
  url?: string;
  path?: string;
  dashboard?: string;
}

/** Commands the worker sends to the browser on {@link JARVIS_UI_TOPIC}. */
export type JarvisUIMessage =
  | { type: 'open_widget'; kind: WidgetKind; title?: string; payload?: unknown; id?: string }
  | { type: 'close_widget'; id?: string; kind?: WidgetKind }
  | { type: 'update_widget'; id?: string; kind?: WidgetKind; payload?: unknown }
  | { type: 'focus_widget'; id?: string; kind?: WidgetKind }
  | { type: 'close_all' };

export const WIDGET_KINDS: WidgetKind[] = [
  'clock',
  'chat',
  'music',
  'search',
  'news',
  'youtube',
  'maps',
  'browser',
  'apps',
  'system',
  'cti',
];

/** Default panel size per widget kind, in CSS pixels. */
export const WIDGET_DEFAULT_SIZE: Record<WidgetKind, { w: number; h: number }> = {
  clock: { w: 250, h: 152 },
  chat: { w: 400, h: 500 },
  music: { w: 360, h: 210 },
  search: { w: 460, h: 540 },
  news: { w: 460, h: 540 },
  youtube: { w: 480, h: 440 },
  maps: { w: 520, h: 420 },
  browser: { w: 820, h: 560 },
  apps: { w: 380, h: 320 },
  system: { w: 340, h: 260 },
  cti: { w: 900, h: 640 },
};

/** Default header text per widget kind. */
export const WIDGET_DEFAULT_TITLE: Record<WidgetKind, string> = {
  clock: 'Chronometer',
  chat: 'Conversation',
  music: 'Music',
  search: 'Search',
  news: 'News Feed',
  youtube: 'YouTube',
  maps: 'Maps',
  browser: 'Browser',
  apps: 'Apps & Services',
  system: 'System Status',
  cti: 'Intelligence',
};
