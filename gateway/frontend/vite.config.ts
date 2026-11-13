import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

import { buildStamp } from "./src/buildStamp";

// The app is the installed progressive web app of the viewer, scoped to `/`,
// so it is served from the root and never from a subfolder.
export default defineConfig({
  base: "/",
  plugins: [react(), buildStamp()],
  server: {
    port: 5173,
    // `npm run dev` serves the app and sends the JSON routes (every one of
    // them under `/api/v1/`) and the document pages to the viewer.
    proxy: {
      "/api": "http://localhost:8000",
      "/documents": "http://localhost:8000",
    },
    // `src/index.css` imports the two master stylesheets of the viewer, which
    // are outside this workspace.
    fs: {
      allow: ["..", "../joshua_gateway"],
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // The polyfill of the module preload is an inline script, and the content
    // policy of the viewer allows no inline script.
    modulePreload: { polyfill: false },
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (!id.includes("node_modules")) {
            return undefined;
          }
          if (id.includes("highlight.js") || id.includes("lowlight")) {
            return "highlight";
          }
          return "vendor";
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["src/test-setup.ts"],
  },
});
