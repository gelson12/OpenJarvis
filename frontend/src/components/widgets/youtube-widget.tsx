// YouTube results: the selected video embedded up top, the rest as a
// clickable list below. Data arrives in widget.payload from the
// worker's search_youtube tool.
//
// Some videos disable embedding (the iframe shows "This content is
// blocked" inside the player) — the embed state can't be detected
// cross-origin, so we expose a "Watch on YouTube" link next to every
// item that always works.

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
      <div className="relative aspect-video w-full shrink-0 bg-black">
        <iframe
          key={selected}
          title="YouTube player"
          src={`https://www.youtube-nocookie.com/embed/${selected}?autoplay=1`}
          className="h-full w-full border-0"
          allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
          allowFullScreen
        />
        {/* Always-available escape hatch when the embed is blocked by the channel. */}
        <a
          href={`https://www.youtube.com/watch?v=${selected}`}
          target="_blank"
          rel="noopener noreferrer"
          className="absolute right-2 top-2 rounded bg-black/70 px-2 py-1 text-[10px] font-mono text-[#3CDFFF] backdrop-blur hover:bg-black/90 hover:text-white"
        >
          Watch on YouTube ↗
        </a>
      </div>
      <ul className="min-h-0 flex-1 divide-y divide-[#3CDFFF]/10 overflow-auto">
        {videos.map((v) => (
          <li key={v.videoId} className="flex items-center">
            <button
              type="button"
              onClick={() => setPicked(v.videoId)}
              className={`flex flex-1 items-center gap-2 px-2 py-1.5 text-left transition-colors hover:bg-[#3CDFFF]/10 ${
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
            <a
              href={`https://www.youtube.com/watch?v=${v.videoId}`}
              target="_blank"
              rel="noopener noreferrer"
              title="Open this video on YouTube"
              className="shrink-0 px-2 text-[#5fb0c6] hover:text-[#3CDFFF]"
              onClick={(e) => e.stopPropagation()}
            >
              ↗
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}
