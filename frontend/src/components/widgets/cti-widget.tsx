// The OpenCTI intelligence panel. An iframe pointing at the
// Railway-hosted OpenCTI instance — the worker supplies the URL in the
// payload (so it can deep-link a specific dashboard), with
// VITE_OPENCTI_URL as the build-time fallback.
//
// Path Y (chosen 2026-05-24): OpenCTI lives on Railway with a public
// HTTPS URL, so this iframe works from ANY browser (phone, ROG, laptop).
// No mixed-content concerns because both pages are HTTPS.
//
// Auth: OpenCTI sets a persistent session cookie on first login. After
// the user has logged in once in the same browser, the iframe is happy.
// For unauthenticated embedding, deploy a public dashboard in OpenCTI
// and whitelist this frontend's host via
// APP__PUBLIC_DASHBOARD_AUTHORIZED_DOMAINS on the OpenCTI service.

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
  const baseUrl = data?.url || import.meta.env.VITE_OPENCTI_URL || '';

  if (!baseUrl) {
    return (
      <WidgetStatus text="OpenCTI URL not configured. Set VITE_OPENCTI_URL on the frontend service or pass `url` in the widget payload." />
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
