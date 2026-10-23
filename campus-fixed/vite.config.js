import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'node:path';
export default defineConfig({
  plugins: [react()],
  build: { outDir: 'dist', rollupOptions: { input: { desktop: resolve('index.html'), campus: resolve('campus.html') } } },
  server: { port: Number(process.env.ATTENTION_WEB_PORT || 5173), proxy: {
    '/api': `http://127.0.0.1:${process.env.CAMPUS_API_PORT || process.env.ATTENTION_API_PORT || 8765}`,
  } },
});
