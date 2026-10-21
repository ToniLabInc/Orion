import { fileURLToPath, URL } from "node:url"
import { defineConfig } from "vitest/config"

// Plain logic only (*.test.ts), in Node: no browser, and none of the HUD's Vite plugins (React, Tailwind).
// Vitest empties every stylesheet import, ?raw included, unless it is listed under css.include: lib/palette.ts reads
// palette.css as text, so that one file is let through.
export default defineConfig({
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  test: {
    include: ["src/**/*.test.ts"],
    environment: "node",
    fileParallelism: false,
    css: { include: [/palette\.css/] },
  },
})
