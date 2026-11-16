import { fileURLToPath } from "node:url";
import { searchForWorkspaceRoot } from "vite";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { info as appIdentity } from "./src/api/openapi.json";

const imageUnavailablePath = fileURLToPath(new URL("../backend/app/templates/image-unavailable.svg", import.meta.url));

export default defineConfig({
  plugins: [react(), { name: "application-identity", transformIndexHtml: (html) => html.replace("__APP_TITLE__", appIdentity.title) }],
  server: {
    port: 5173,
    fs: {
      allow: [
        searchForWorkspaceRoot(fileURLToPath(new URL(".", import.meta.url))),
        // Vite checks both the original file and the inline module ID.
        imageUnavailablePath,
        `${imageUnavailablePath}?inline`
      ]
    },
    proxy: {
      "/api": "http://localhost:8000",
      "/health": "http://localhost:8000"
    }
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts"
  }
});
