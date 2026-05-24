// Tiny pub-sub for `widget_volume` messages. The worker sends commands
// like { type: 'widget_volume', kind: 'youtube', action: 'mute' } on the
// jarvis-ui topic; useJarvisUIChannel intercepts those and forwards them
// here so individual widgets (which own their player handles) can react
// without routing playback state through the widget store.

import type { WidgetKind } from './protocol';

export interface WidgetVolumeCommand {
  kind: WidgetKind;
  action: 'mute' | 'unmute' | 'set';
  level?: number;
}

type Listener = (cmd: WidgetVolumeCommand) => void;

const listeners = new Map<WidgetKind, Set<Listener>>();

export function subscribeWidgetVolume(
  kind: WidgetKind,
  fn: Listener
): () => void {
  let set = listeners.get(kind);
  if (!set) {
    set = new Set();
    listeners.set(kind, set);
  }
  set.add(fn);
  return () => {
    set?.delete(fn);
    if (set && set.size === 0) listeners.delete(kind);
  };
}

export function publishWidgetVolume(cmd: WidgetVolumeCommand): void {
  const set = listeners.get(cmd.kind);
  if (!set) return;
  for (const fn of set) {
    try {
      fn(cmd);
    } catch (err) {
      console.warn('[widget-volume] listener threw', err);
    }
  }
}
