// Draggable, semi-transparent HUD panel that frames every widget.
//
// Dragging goes through the store (onMove) rather than motion's drag,
// so the mouse and the gesture system share ONE position code path —
// the gesture controller calls the same onMove to grab-and-drag.

import { useRef, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react';
import { motion } from 'motion/react';
import { X } from 'lucide-react';
import type { WidgetInstance } from '@/lib/jarvis-ui/protocol';
import { cn } from '@/lib/utils';

interface WidgetShellProps {
  widget: WidgetInstance;
  /** True while the gesture cursor is hovering this widget. */
  highlighted: boolean;
  children: ReactNode;
  onClose: () => void;
  onFocus: () => void;
  onMove: (x: number, y: number) => void;
}

export function WidgetShell({
  widget,
  highlighted,
  children,
  onClose,
  onFocus,
  onMove,
}: WidgetShellProps) {
  // Pointer offset captured at drag start (cursor → widget origin).
  const dragOffset = useRef<{ dx: number; dy: number } | null>(null);

  function onHeaderPointerDown(e: ReactPointerEvent) {
    if (e.button !== 0) return;
    dragOffset.current = { dx: e.clientX - widget.x, dy: e.clientY - widget.y };
    e.currentTarget.setPointerCapture(e.pointerId);
  }
  function onHeaderPointerMove(e: ReactPointerEvent) {
    const off = dragOffset.current;
    if (!off) return;
    onMove(e.clientX - off.dx, e.clientY - off.dy);
  }
  function onHeaderPointerUp(e: ReactPointerEvent) {
    dragOffset.current = null;
    e.currentTarget.releasePointerCapture?.(e.pointerId);
  }

  return (
    <motion.section
      onPointerDownCapture={onFocus}
      initial={{ opacity: 0, scale: 0.92 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.92 }}
      transition={{ type: 'spring', stiffness: 520, damping: 38 }}
      style={{
        left: widget.x,
        top: widget.y,
        width: widget.w,
        height: widget.h,
        zIndex: widget.z,
      }}
      className={cn(
        'pointer-events-auto absolute flex flex-col overflow-hidden rounded-xl border',
        'bg-[#04101a]/85 backdrop-blur-md transition-shadow',
        highlighted
          ? 'border-[#3CDFFF] shadow-[0_0_48px_-6px_rgba(60,223,255,0.8)]'
          : 'border-[#3CDFFF]/30 shadow-[0_0_42px_-10px_rgba(60,223,255,0.45)]'
      )}
    >
      {/* corner brackets */}
      <span className="pointer-events-none absolute top-0 left-0 h-3 w-3 border-t border-l border-[#3CDFFF]/70" />
      <span className="pointer-events-none absolute top-0 right-0 h-3 w-3 border-t border-r border-[#3CDFFF]/70" />
      <span className="pointer-events-none absolute bottom-0 left-0 h-3 w-3 border-b border-l border-[#3CDFFF]/70" />
      <span className="pointer-events-none absolute right-0 bottom-0 h-3 w-3 border-r border-b border-[#3CDFFF]/70" />

      {/* title bar — drag handle */}
      <header
        onPointerDown={onHeaderPointerDown}
        onPointerMove={onHeaderPointerMove}
        onPointerUp={onHeaderPointerUp}
        onPointerCancel={onHeaderPointerUp}
        className="flex cursor-grab items-center justify-between gap-2 border-b border-[#3CDFFF]/20 bg-[#3CDFFF]/10 px-3 py-1.5 select-none active:cursor-grabbing"
      >
        <span className="font-mono text-[11px] tracking-[0.18em] text-[#bfeefc] uppercase">
          {widget.title}
        </span>
        <button
          type="button"
          aria-label={`Close ${widget.title}`}
          onClick={onClose}
          onPointerDown={(e) => e.stopPropagation()}
          className="grid h-5 w-5 place-content-center rounded text-[#7fd3e6] transition-colors hover:bg-[#3CDFFF]/20 hover:text-white"
        >
          <X size={13} strokeWidth={2.5} />
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-auto text-[#dff4fb]">{children}</div>
    </motion.section>
  );
}
