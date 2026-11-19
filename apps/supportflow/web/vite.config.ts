import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  root: "smoke",
  plugins: [react()],
  build: { outDir: "../dist/env-smoke", emptyOutDir: true },
});
