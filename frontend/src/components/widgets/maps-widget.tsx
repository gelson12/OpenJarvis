// A location or directions on an embedded map. Uses the keyless
// maps.google.com embed — no API key required. Data arrives in
// widget.payload from the worker's show_map tool.

import type { MapsPayload, WidgetComponentProps } from '@/lib/jarvis-ui/protocol';
import { WidgetStatus } from './widget-status';

export function MapsWidget({ widget }: WidgetComponentProps) {
  const data = widget.payload as MapsPayload | undefined;
  const query = data?.query?.trim();

  if (!query) return <WidgetStatus text="No location specified." />;

  return (
    <iframe
      title={`Map of ${query}`}
      src={`https://maps.google.com/maps?q=${encodeURIComponent(query)}&z=12&output=embed`}
      className="h-full w-full border-0"
      loading="lazy"
      referrerPolicy="no-referrer-when-downgrade"
    />
  );
}
