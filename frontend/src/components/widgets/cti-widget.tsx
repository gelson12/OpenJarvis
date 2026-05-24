// The OpenCTI intelligence panel. An iframe pointing at a self-hosted
// OpenCTI instance — the worker supplies the URL in the payload (so it
// can deep-link a specific dashboard), with VITE_OPENCTI_URL as the
// build-time fallback, and `http://localhost:8080` as the final default
// (matches the laptop-Docker deployment recipe).
//
// localhost note: modern browsers (Chrome/Edge/Firefox) treat
// http://localhost as a Secure Context, so this HTTPS-served page CAN
// iframe http://localhost:8080 without mixed-content blocking. The
// iframe therefore only resolves when the user views Jarvis from the
// same machine that runs OpenCTI (the laptop). When viewing from
// another device, the iframe shows a connection-refused state —
// expected for the bridge-proxy architecture.
//
// Auth: OpenCTI sets a persistent session cookie on first login. After
// the user has logged in once in the same browser, this iframe just
// works.

import type { CTIPayload, WidgetComponentProps } from '@/lib/jarvis-ui/protocol';
import { WidgetStatus } from './widget-status';

function joinUrl(base: string, path?: string): string {
  const b = base.replace(/\/+$/, '');
  if (!path) return b;
  const p = path.startsWith('/') ? path : `/${path}`;
  return `${b}${p}`;
}

export function CTIWidget({ widget }: WidgetComponentProps) {
  const data = widget.payload as CTIPayload | undefined;
  const baseUrl =
    data?.url ||
    import.meta.env.VITE_OPENCTI_URL ||
    'http://localhost:8080';

  if (!baseUrl) {
    return (
      <WidgetStatus text="OpenCTI URL not configured. Set VITE_OPENCTI_URL or pass `url` in the widget payload." />
    );
  }

  const src = joinUrl(baseUrl, data?.path);

  return (
    <iframe
      title="OpenCTI Intelligence"
      src={src}
      className="h-full w-full border-0"
      loading="lazy"
      referrerPolicy="no-referrer-when-downgrade"
      // OpenCTI loads worker scripts, postMessage, and dashboard
      // visualisations — give it the standard permissive iframe sandbox.
      sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-modals allow-downloads allow-popups-to-escape-sandbox"
    />
  );
}
