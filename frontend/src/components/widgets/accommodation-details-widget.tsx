'use client';

// Phase 3.2.d — "Search context" details panel. A small sibling widget
// to the main accommodation carousel, sent on every search so the user
// can keep the query shape visible (where/when/who/how many results/
// top pick) while scrolling the carousel.
//
// Pure read-only renderer. No interactivity in v1.

import type {
  AccommodationDetailsPayload,
  WidgetComponentProps,
} from '@/lib/jarvis-ui/protocol';
import { WidgetStatus } from './widget-status';

function formatPrice(amount: number, currency: string): string {
  try {
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency,
      maximumFractionDigits: 0,
    }).format(amount);
  } catch {
    return `${amount.toFixed(0)} ${currency}`;
  }
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-2 px-3 py-2 text-xs">
      <span className="text-[#5fb0c6] uppercase tracking-wide">{label}</span>
      <span className="truncate text-[#cdf2fb]">{value}</span>
    </div>
  );
}

export function AccommodationDetailsWidget({ widget }: WidgetComponentProps) {
  const data = widget.payload as AccommodationDetailsPayload | undefined;
  if (!data) return <WidgetStatus text="Awaiting search…" />;

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-[#3CDFFF]/10 px-3 py-2">
        <div className="text-[10px] uppercase tracking-wider text-[#5fb0c6]">
          Destination
        </div>
        <div className="text-lg font-medium text-[#cdf2fb]">{data.location}</div>
      </div>
      <div className="flex-1 divide-y divide-[#3CDFFF]/10 overflow-y-auto">
        <Row label="Check-in" value={data.check_in} />
        <Row label="Check-out" value={data.check_out} />
        <Row label="Nights" value={String(data.nights)} />
        <Row label="Guests" value={String(data.guests)} />
        <Row label="Providers" value={data.providers.join(', ') || '—'} />
        <Row label="Results" value={String(data.count)} />
      </div>
      <div className="border-t border-[#3CDFFF]/10 px-3 py-3">
        <div className="mb-1 text-[10px] uppercase tracking-wider text-[#5fb0c6]">
          Top pick
        </div>
        <div className="truncate text-sm text-[#cdf2fb]">{data.top_pick.name}</div>
        <div className="mt-1 text-base font-semibold text-[#3CDFFF]">
          {formatPrice(data.top_pick.price_total, data.top_pick.price_currency)}
        </div>
      </div>
    </div>
  );
}
