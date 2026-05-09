import path from 'path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { VitePWA } from 'vite-plugin-pwa';
import { viteStaticCopy } from 'vite-plugin-static-copy';

// Copy @ricky0123/vad-web's worklet + ONNX models and onnxruntime-web's
// WASM binaries into the build output so they're served from the same
// origin at runtime. Without this, MicVAD.new() can't find its assets
// and barge-in silently never fires. The library expects them at the
// `baseAssetPath` URL we configure in useBargeInVAD ('/' by default).
const vadAssets = viteStaticCopy({
  targets: [
    {
      src: 'node_modules/@ricky0123/vad-web/dist/vad.worklet.bundle.min.js',
      dest: '.',
    },
    {
      src: 'node_modules/@ricky0123/vad-web/dist/silero_vad_legacy.onnx',
      dest: '.',
    },
    {
      src: 'node_modules/@ricky0123/vad-web/dist/silero_vad_v5.onnx',
      dest: '.',
    },
    {
      src: 'node_modules/onnxruntime-web/dist/*.wasm',
      dest: '.',
    },
  ],
});

export default defineConfig({
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  plugins: [
    react(),
    tailwindcss(),
    vadAssets,
    VitePWA({
      registerType: 'autoUpdate',
      manifest: {
        name: 'OpenJarvis',
        short_name: 'Jarvis',
        description: 'On-device AI assistant',
        theme_color: '#161618',
        background_color: '#161618',
        display: 'standalone',
        icons: [
          { src: 'pwa-192x192.png', sizes: '192x192', type: 'image/png' },
          { src: 'pwa-512x512.png', sizes: '512x512', type: 'image/png' },
        ],
      },
      workbox: {
        globPatterns: ['**/*.{js,css,html,ico,png,svg}'],
        navigateFallbackDenylist: [/^\/v1\//, /^\/health/, /^\/dashboard/],
      },
    }),
  ],
  build: {
    outDir: '../src/openjarvis/server/static',
    emptyOutDir: true,
    minify: 'esbuild',
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom'],
          markdown: ['react-markdown', 'rehype-highlight', 'remark-gfm'],
          charts: ['recharts'],
          router: ['react-router'],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/v1': process.env.VITE_API_URL || 'http://localhost:8000',
      '/health': process.env.VITE_API_URL || 'http://localhost:8000',
    },
  },
});
