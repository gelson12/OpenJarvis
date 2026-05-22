// Maps a widget kind to its React component. Kinds without a dedicated
// component yet fall back to PlaceholderWidget (music, apps, system).

import { type ComponentType } from 'react';
import type { WidgetComponentProps, WidgetKind } from '@/lib/jarvis-ui/protocol';
import { BrowserWidget } from './browser-widget';
import { ChatWidget } from './chat-widget';
import { ClockWidget } from './clock-widget';
import { MapsWidget } from './maps-widget';
import { NewsWidget } from './news-widget';
import { PlaceholderWidget } from './placeholder-widget';
import { SearchWidget } from './search-widget';
import { YouTubeWidget } from './youtube-widget';

const REGISTRY: Partial<Record<WidgetKind, ComponentType<WidgetComponentProps>>> = {
  clock: ClockWidget,
  chat: ChatWidget,
  search: SearchWidget,
  news: NewsWidget,
  youtube: YouTubeWidget,
  maps: MapsWidget,
  browser: BrowserWidget,
};

export function getWidgetComponent(
  kind: WidgetKind
): ComponentType<WidgetComponentProps> {
  return REGISTRY[kind] ?? PlaceholderWidget;
}
