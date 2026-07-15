import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/upload": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/model-routes": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/assistant": {
        target: "http://localhost:8000",
        changeOrigin: true,
        // `/assistant` (bare, with optional query) is a client-side SPA route,
        // not an API endpoint. Don't proxy it to the backend (which has no such
        // route and would 404 on page refresh); let Vite serve index.html so the
        // router can take over. Sub-paths like /assistant/sessions are proxied.
        bypass(req) {
          const raw = req.url ?? "";
          const path = raw.split("?", 1)[0];
          if (path === "/assistant") {
            return "/index.html";
          }
        },
      },
    },
  },
  build: {
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("node_modules")) {
            if (
              id.includes("klinecharts") ||
              id.includes("recharts") ||
              id.includes("recharts/") ||
              id.includes("klinecharts/")
            ) {
              return "charting";
            }
            if (
              id.includes("@uiw/react-codemirror") ||
              id.includes("@codemirror/") ||
              id.includes("react-syntax-highlighter")
            ) {
              return "editor";
            }
            if (id.includes("@ant-design/icons")) {
              return "antd-vendor";
            }
            if (id.includes("antd") || id.includes("ant-design")) {
              return "antd-vendor";
            }
            if (id.includes("react-dom") || id.includes("react/")) {
              return "react-vendor";
            }
          }
        },
      },
    },
  },
});
