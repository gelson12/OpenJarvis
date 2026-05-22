// YouTube results: the selected video embedded up top, the rest as a
// clickable list below. Data arrives in widget.payload from the
// worker's search_youtube tool.

import { useState } from 'react';
import type { WidgetComponentProps, YouTubePayload } from '@/lib/jarvis-ui/protocol';
import { WidgetStatus } from './widget-status';

export function YouTubeWidget({ widget }: WidgetComponentProps) {
  const data = widget.payload as YouTubePayload | undefined;
  const videos = data?.videos ?? [];
  const [picked, setPicked] = useState<string | null>(null);

  if (!data) return <WidgetStatus text="Searching YouTube…" />;
  if (videos.length === 0) {
    return <WidgetStatus text={`No videos for "${data.query}".`} />;
  }

  // Fall back to the first video if the picked one isn't in this list
  // (the payload may have been replaced by a fresh search).
  const selected =
    picked && videos.some((v) => v.videoId === picked) ? picked : videos[0].videoId;

  return (
    <div className="flex h-full flex-col">
      <div className="aspect-video w-full shrink-0 bg-black">
        <iframe
          key={selected}
          title="YouTube player"
          src={`https://www.youtube-nocookie.com/embed/${selected}`}
          className="h-full w-full border-0"
          allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
          allowFullScreen
        />
      </div>
      <ul className="min-h-0 flex-1 divide-y divide-[#3CDFFF]/10 overflow-auto">
        {videos.map((v) => (
          <li key={v.videoId}>
            <button
              type="button"
              onClick={() => setPicked(v.videoId)}
              className={`flex w-full items-center gap-2 px-2 py-1.5 text-left transition-colors hover:bg-[#3CDFFF]/10 ${
                v.videoId === selected ? 'bg-[#3CDFFF]/15' : ''
              }`}
            >
              {v.thumbnail && (
                <img
                  src={v.thumbnail}
                  alt=""
                  className="h-9 w-16 shrink-0 rounded object-cover"
                />
              )}
              <span className="min-w-0">
                <span className="line-clamp-1 text-xs text-[#cdf2fb]">{v.title}</span>
                <span className="line-clamp-1 font-mono text-[10px] text-[#5fb0c6]">
                  {v.channel}
                </span>
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
