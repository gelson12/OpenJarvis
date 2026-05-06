/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_URL: string;
  /**
   * Optional build-time API key for local development.
   * In production (Railway) this is intentionally left unset — the key is
   * fetched at runtime from the /v1/config endpoint instead.
   */
  readonly VITE_OPENJARVIS_API_KEY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
