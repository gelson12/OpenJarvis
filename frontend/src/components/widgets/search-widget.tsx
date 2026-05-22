// Web-search results as a scrollable list of cards. Data arrives in
// widget.payload from the worker's web_search tool.

import type { SearchPayload, WidgetComponentProps } from '@/lib/jarvis-ui/protocol';
import { WidgetStatus } from './widget-status';

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return url;
  }
}

export function SearchWidget({ widget }: WidgetComponentProps) {
  const data = widget.payload as SearchPayload | undefined;

  if (!data) return <WidgetStatus text="Awaiting search…" />;
  if (data.results.length === 0) {
    return <WidgetStatus text={`No results for "${data.query}".`} />;
  }

  return (
    <ul className="divide-y divide-[#3CDFFF]/10">
      {data.results.map((r, i) => (
        <li key={i}>
          <a
            href={r.url}
            target="_blank"
            rel="noreferrer"
            className="block px-3 py-2.5 transition-colors hover:bg-[#3CDFFF]/10"
          >
            <div className="truncate text-sm font-medium text-[#cdf2fb]">{r.title}</div>
            <div className="truncate font-mono text-[10px] text-[#5fb0c6]">
              {hostOf(r.url)}
            </div>
            {r.snippet && (
              <p className="mt-1 line-clamp-2 text-xs text-[#9fd0dd]">{r.snippet}</p>
            )}
          </a>
        </li>
      ))}
    </ul>
  );
}
