// Live remote-browser widget. The worker streams JPEG frames of a real
// Chromium page over JARVIS_BROWSER_TOPIC (chunked ~12 KB at a time);
// this widget reassembles them and relays clicks, scrolls, keys and
// navigation back over the same topic.

import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowLeft, RotateCw } from 'lucide-react';
import { useDataChannel, useRoomContext } from '@livekit/components-react';
import { JARVIS_BROWSER_TOPIC } from '@/lib/jarvis-ui/protocol';
import { WidgetStatus } from './widget-status';

interface FrameMsg {
  t: string;
  id: string;
  seq: number;
  total: number;
  data: string;
  url?: string;
}

export function BrowserWidget() {
  const room = useRoomContext();
  const [imgSrc, setImgSrc] = useState<string | null>(null);
  const [pageUrl, setPageUrl] = useState('');
  const [urlInput, setUrlInput] = useState('');
  const pending = useRef<{ id: string; parts: string[]; url: string } | null>(null);
  const urlFieldRef = useRef<HTMLInputElement>(null);

  // ── Receive streamed frames (reassembled from chunks) ───────────
  const onData = useCallback((msg: { payload: Uint8Array }) => {
    let f: FrameMsg;
    try {
      f = JSON.parse(new TextDecoder().decode(msg.payload)) as FrameMsg;
    } catch {
      return;
    }
    if (f.t !== 'frame') return;
    if (f.seq === 0) pending.current = { id: f.id, parts: [], url: f.url ?? '' };
    const p = pending.current;
    if (!p || p.id !== f.id) return;
    p.parts[f.seq] = f.data;
    if (f.seq === f.total - 1) {
      setImgSrc('data:image/jpeg;base64,' + p.parts.join(''));
      setPageUrl(p.url);
      // Don't clobber the address bar while the user is editing it.
      if (document.activeElement !== urlFieldRef.current) setUrlInput(p.url);
      pending.current = null;
    }
  }, []);
  useDataChannel(JARVIS_BROWSER_TOPIC, onData);

  // ── Send interactions back to the worker's Playwright page ──────
  const send = useCallback(
    (event: Record<string, unknown>) => {
      try {
        room.localParticipant.publishData(
          new TextEncoder().encode(JSON.stringify(event)),
          { reliable: true, topic: JARVIS_BROWSER_TOPIC }
        );
      } catch {
        /* room not connected */
      }
    },
    [room]
  );

  // Close the worker-side browser session when the widget is dismissed.
  useEffect(() => () => send({ action: 'close' }), [send]);

  function onImageClick(e: React.MouseEvent<HTMLImageElement>) {
    const r = e.currentTarget.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    send({
      action: 'click',
      x: (e.clientX - r.left) / r.width,
      y: (e.clientY - r.top) / r.height,
    });
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (document.activeElement === urlFieldRef.current) return; // typing a URL
    const named = ['Enter', 'Backspace', 'Tab', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'];
    if (e.key.length !== 1 && !named.includes(e.key)) return;
    e.preventDefault();
    send({ action: 'key', key: e.key });
  }

  function submitUrl(e: React.FormEvent) {
    e.preventDefault();
    const u = urlInput.trim();
    if (u) send({ action: 'navigate', url: u });
    urlFieldRef.current?.blur();
  }

  return (
    <div className="flex h-full flex-col" onKeyDown={onKeyDown}>
      {/* toolbar */}
      <div className="flex shrink-0 items-center gap-1.5 border-b border-[#3CDFFF]/15 bg-[#3CDFFF]/5 px-2 py-1.5">
        <button
          type="button"
          aria-label="Back"
          onClick={() => send({ action: 'back' })}
          className="grid h-6 w-6 shrink-0 place-content-center rounded text-[#7fd3e6] transition-colors hover:bg-[#3CDFFF]/15 hover:text-white"
        >
          <ArrowLeft size={14} />
        </button>
        <button
          type="button"
          aria-label="Reload"
          onClick={() => send({ action: 'reload' })}
          className="grid h-6 w-6 shrink-0 place-content-center rounded text-[#7fd3e6] transition-colors hover:bg-[#3CDFFF]/15 hover:text-white"
        >
          <RotateCw size={13} />
        </button>
        <form onSubmit={submitUrl} className="min-w-0 flex-1">
          <input
            ref={urlFieldRef}
            value={urlInput}
            onChange={(e) => setUrlInput(e.target.value)}
            placeholder="Enter a web address"
            spellCheck={false}
            className="w-full rounded bg-[#02080e] px-2 py-1 font-mono text-[11px] text-[#cdf2fb] outline-none placeholder:text-[#4d7a88] focus:ring-1 focus:ring-[#3CDFFF]/50"
          />
        </form>
      </div>
      {/* page surface */}
      <div className="flex min-h-0 flex-1 items-center justify-center bg-black" tabIndex={0}>
        {imgSrc ? (
          <img
            src={imgSrc}
            alt={pageUrl || 'browser page'}
            draggable={false}
            onClick={onImageClick}
            onWheel={(e) => send({ action: 'scroll', dy: e.deltaY })}
            className="max-h-full max-w-full cursor-pointer object-contain select-none"
          />
        ) : (
          <WidgetStatus text="Opening browser…" />
        )}
      </div>
    </div>
  );
}
