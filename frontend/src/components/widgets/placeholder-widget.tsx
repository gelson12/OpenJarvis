// Fallback for widget kinds that don't have a real component yet, so
// the worker can summon any kind without crashing the UI.

import type { WidgetComponentProps } from '@/lib/jarvis-ui/protocol';

export function PlaceholderWidget({ widget }: WidgetComponentProps) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2.5 px-4 text-center">
      <div className="h-8 w-8 animate-spin rounded-full border-2 border-[#3CDFFF]/30 border-t-[#3CDFFF]" />
      <p className="font-mono text-[11px] tracking-[0.18em] text-[#7fd3e6] uppercase">
        {widget.kind} module
      </p>
      <p className="text-xs text-[#9fd0dd]">Coming online in a later phase.</p>
    </div>
  );
}
