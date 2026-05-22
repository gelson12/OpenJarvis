// News headlines as a scrollable list. Data arrives in widget.payload
// from the worker's show_news tool (keyless Google News RSS).

import type { NewsPayload, WidgetComponentProps } from '@/lib/jarvis-ui/protocol';
import { WidgetStatus } from './widget-status';

function timeAgo(value: string): string {
  const t = Date.parse(value);
  if (Number.isNaN(t)) return '';
  const mins = Math.round((Date.now() - t) / 60000);
  if (mins < 60) return `${Math.max(mins, 1)}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

export function NewsWidget({ widget }: WidgetComponentProps) {
  const data = widget.payload as NewsPayload | undefined;

  if (!data) return <WidgetStatus text="Fetching headlines…" />;
  if (data.articles.length === 0) {
    return <WidgetStatus text="No headlines available right now." />;
  }

  return (
    <ul className="divide-y divide-[#3CDFFF]/10">
      {data.articles.map((a, i) => (
        <li key={i}>
          <a
            href={a.url}
            target="_blank"
            rel="noreferrer"
            className="block px-3 py-2.5 transition-colors hover:bg-[#3CDFFF]/10"
          >
            <div className="line-clamp-2 text-sm font-medium text-[#cdf2fb]">
              {a.title}
            </div>
            <div className="mt-1 flex items-center gap-1.5 font-mono text-[10px] text-[#5fb0c6] uppercase">
              <span className="truncate">{a.source}</span>
              {a.published && <span className="shrink-0">· {timeAgo(a.published)}</span>}
            </div>
          </a>
        </li>
      ))}
    </ul>
  );
}
