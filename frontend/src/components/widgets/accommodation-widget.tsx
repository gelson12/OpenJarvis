'use client';

// Accommodation search results / post-booking payment-link card.
//
// Two modes:
//   1. Search-results carousel — `properties` is set. Renders a scrollable
//      list of property cards with thumbnail, name, rating, price, "Book"
//      button. Tapping a card opens the property's checkout in a new tab.
//   2. Payment-link card — `checkout_url` is set (Telegram handoff fell
//      back to HUD delivery). Renders a single "Complete payment" CTA.
//
// PCI design: card data NEVER touches this component or the worker. The
// `checkout_url` always points to the provider's hosted checkout page; we
// open it in `target="_blank"` so the user pays on the provider's domain.

import type {
  AccommodationPayload,
  AccommodationProperty,
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

function PropertyCard({ p }: { p: AccommodationProperty }) {
  const thumb = p.images[0];
  return (
    <li className="px-3 py-2.5">
      <div className="flex gap-3">
        {thumb ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={thumb}
            alt={p.name}
            className="h-20 w-28 flex-shrink-0 rounded object-cover"
            loading="lazy"
          />
        ) : (
          <div className="h-20 w-28 flex-shrink-0 rounded bg-[#3CDFFF]/10" />
        )}
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium text-[#cdf2fb]">{p.name}</div>
          <div className="truncate text-[10px] text-[#5fb0c6]">{p.address}</div>
          <div className="mt-1 flex items-baseline justify-between gap-2">
            <div className="text-base font-semibold text-[#3CDFFF]">
              {formatPrice(p.price_total, p.price_currency)}
            </div>
            {p.rating != null && (
              <div className="text-[11px] text-[#9fd0dd]">
                ★ {p.rating.toFixed(1)}
                {p.review_count != null && p.review_count > 0 && (
                  <span className="ml-1 text-[#5fb0c6]">({p.review_count})</span>
                )}
              </div>
            )}
          </div>
          <div className="mt-1 text-[10px] uppercase tracking-wide text-[#5fb0c6]">
            via {p.provider_id}
          </div>
        </div>
      </div>
    </li>
  );
}

function PaymentLinkCard({ data }: { data: AccommodationPayload }) {
  return (
    <div className="flex h-full flex-col items-center justify-center p-6 text-center">
      <div className="mb-2 text-xs uppercase tracking-wider text-[#5fb0c6]">
        Booking ready
      </div>
      <div className="mb-1 text-lg font-medium text-[#cdf2fb]">{data.query}</div>
      {data.price_total != null && data.price_currency && (
        <div className="mb-5 text-2xl font-semibold text-[#3CDFFF]">
          {formatPrice(data.price_total, data.price_currency)}
        </div>
      )}
      <a
        href={data.checkout_url}
        target="_blank"
        rel="noreferrer noopener"
        className="rounded-md bg-[#3CDFFF]/20 px-5 py-2.5 text-sm font-medium text-[#3CDFFF] transition-colors hover:bg-[#3CDFFF]/30"
      >
        Complete payment securely →
      </a>
      <div className="mt-4 max-w-xs text-[10px] leading-snug text-[#5fb0c6]">
        Card details are entered on the provider&apos;s page only. Friday never sees them.
      </div>
    </div>
  );
}

export function AccommodationWidget({ widget }: WidgetComponentProps) {
  const data = widget.payload as AccommodationPayload | undefined;
  if (!data) return <WidgetStatus text="Awaiting results…" />;

  // Payment-link mode (Telegram fallback).
  if (data.checkout_url) {
    return <PaymentLinkCard data={data} />;
  }

  const properties = data.properties ?? [];
  if (properties.length === 0) {
    return <WidgetStatus text={`No stays found for "${data.query}".`} />;
  }

  return (
    <div className="flex h-full flex-col">
      {data.check_in && data.check_out && (
        <div className="border-b border-[#3CDFFF]/10 px-3 py-1.5 text-[10px] text-[#5fb0c6]">
          {data.check_in} → {data.check_out} · {properties.length} properties
        </div>
      )}
      <ul className="flex-1 divide-y divide-[#3CDFFF]/10 overflow-y-auto">
        {properties.map((p, i) => (
          <PropertyCard key={`${p.provider_id}-${p.external_id}-${i}`} p={p} />
        ))}
      </ul>
    </div>
  );
}
