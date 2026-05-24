// Embeds a v0.dev-generated website preview as a clean iframe.
// The worker's build_website handler publishes this widget after
// extracting the vusercontent.net preview URL from the v0 API
// response. CSP frame-src is extended in middleware.py to allow
// *.vusercontent.net / v0.dev / *.vercel.app hosts.

import type { WidgetComponentProps, SitePayload } from '@/lib/jarvis-ui/protocol';
import { WidgetStatus } from './widget-status';

export function SiteWidget({ widget }: WidgetComponentProps) {
  const data = widget.payload as SitePayload | undefined;
  if (!data?.url) return <WidgetStatus text="Waiting for v0…" />;

  return (
    <div className="relative h-full w-full bg-black">
      <iframe
        title={`Generated site — ${data.prompt}`}
        src={data.url}
        className="h-full w-full border-0"
        // sandbox lets the generated page render and accept input,
        // but blocks it from navigating us or opening popups.
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
        allow="clipboard-write"
      />
      {/* Always-available escape hatch — opens the live URL in a tab. */}
      <a
        href={data.url}
        target="_blank"
        rel="noopener noreferrer"
        className="absolute right-2 top-2 rounded bg-black/70 px-2 py-1 text-[10px] font-mono text-[#3CDFFF] backdrop-blur hover:bg-black/90 hover:text-white"
      >
        Open ↗
      </a>
    </div>
  );
}
