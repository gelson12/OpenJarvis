// A location or directions on an embedded map.
//
// We deliberately do NOT use Google's legacy keyless embed
// (`maps.google.com/maps?...&output=embed`) any more — Google has
// been progressively blocking it since 2024 and it now renders as a
// red "embed denied" prohibition icon for most queries.
//
// Instead: geocode the spoken query through Nominatim
// (OpenStreetMap's free service, CORS-friendly, no API key) and
// embed the standard OSM iframe with a bbox + marker. Same UX,
// works without any user setup, and Nominatim is what the worker's
// `geocode_search` tool already uses server-side
// (src/openjarvis/tools/geocode_tools.py) so we stay on one source.
//
// Limitation: Nominatim is weaker than Google Places for "nearest X"
// proximity queries (no user location, no live POI categories). When
// the user asks "nearest coffee" we still show the best Nominatim
// match — usually a city / district result rather than a specific
// shop. Adding browser geolocation + Overpass for category lookups
// is the next iteration; for now this is strictly better than the
// blocked Google iframe.

import { useEffect, useState } from 'react';
import type { MapsPayload, WidgetComponentProps } from '@/lib/jarvis-ui/protocol';
import { WidgetStatus } from './widget-status';

interface NominatimResult {
  lat: string;
  lon: string;
  boundingbox: [string, string, string, string]; // [south, north, west, east]
  display_name: string;
}

interface MapState {
  url: string;
  label: string;
}

export function MapsWidget({ widget }: WidgetComponentProps) {
  const data = widget.payload as MapsPayload | undefined;
  const query = data?.query?.trim();
  const [state, setState] = useState<MapState | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!query) {
      setState(null);
      setError(null);
      return;
    }
    setState(null);
    setError(null);

    const ac = new AbortController();
    const url =
      'https://nominatim.openstreetmap.org/search' +
      `?format=json&limit=1&q=${encodeURIComponent(query)}`;

    fetch(url, {
      signal: ac.signal,
      headers: { Accept: 'application/json' },
    })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<NominatimResult[]>;
      })
      .then((results) => {
        if (!results || results.length === 0) {
          setError(`No map results for "${query}".`);
          return;
        }
        const r0 = results[0];
        const lat = parseFloat(r0.lat);
        const lon = parseFloat(r0.lon);
        const [sStr, nStr, wStr, eStr] = r0.boundingbox;
        // Nominatim sends a tight bbox; pad it slightly so the
        // marker isn't pinned to the very edge of the viewport.
        const south = parseFloat(sStr);
        const north = parseFloat(nStr);
        const west = parseFloat(wStr);
        const east = parseFloat(eStr);
        const padLat = Math.max(0.005, (north - south) * 0.2);
        const padLon = Math.max(0.005, (east - west) * 0.2);
        const bbox =
          `${west - padLon},${south - padLat},` +
          `${east + padLon},${north + padLat}`;
        const embed =
          'https://www.openstreetmap.org/export/embed.html' +
          `?bbox=${encodeURIComponent(bbox)}&layer=mapnik` +
          `&marker=${lat},${lon}`;
        setState({ url: embed, label: r0.display_name });
      })
      .catch((err: unknown) => {
        if (err instanceof Error && err.name === 'AbortError') return;
        const msg = err instanceof Error ? err.message : String(err);
        setError(`Could not load map: ${msg}`);
      });

    return () => ac.abort();
  }, [query]);

  if (!query) return <WidgetStatus text="No location specified." />;
  if (error) return <WidgetStatus text={error} />;
  if (!state) return <WidgetStatus text={`Locating "${query}"…`} />;

  return (
    <iframe
      title={`Map of ${state.label}`}
      src={state.url}
      className="h-full w-full border-0"
      loading="lazy"
      referrerPolicy="no-referrer-when-downgrade"
    />
  );
}
