import { readFileSync } from "node:fs"
import { fileURLToPath, URL } from "node:url"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig, type Plugin } from "vite"

// In development Vite serves the HUD and passes the API and the live socket on to Orion. Host and Origin go through
// unchanged (no changeOrigin): Orion only answers its own pages on this PC, and to it these are 127.0.0.1:5173's.
// ORION_URL points it at another Orion (say, an isolated one for development on another port).
const orion = process.env.ORION_URL ?? "http://127.0.0.1:8765"

/**
 * palette.css as lib/palette.ts reads it (`?raw`, in a worker too): without its comments and the spaces the
 * parser skips anyway, so each bundle that carries it is about 0.8 kB lighter.
 * @returns the plugin
 */
function paletteText(): Plugin {
  return {
    name: "orion:palette-text",
    enforce: "pre",
    load(id) {
      const [file, query] = id.split("?")
      if (query !== "raw" || !file.endsWith("/palette.css")) return
      const css = readFileSync(file, "utf8")
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .replace(/\s+/g, " ")
        .replace(/ ?([{};:,]) ?/g, "$1") // the parser allows no space here; between oklch's numbers one stays
        .trim()
      return `export default ${JSON.stringify(css)}`
    },
  }
}

/**
 * The Core's shaders (the GLSL in src/core/shaders.ts's template literals tagged with a glsl comment) without their
 * comments, indentation, blank lines and the spaces round punctuation, in its worker and its on-page fallback. The
 * newlines stay (GLSL's preprocessor needs them), and GLSL has no strings, so `//` always starts a comment. Its
 * literals hold no block comments, and `${HEAD}`-style interpolations stand on lines of their own.
 * @returns the plugin
 */
function glslText(): Plugin {
  // a line without its comment, its runs of spaces, and the spaces round punctuation that can't join two tokens (never
  // round + or -, so a unary minus can't turn into --)
  const tidy = (line: string) =>
    line.trimStart().startsWith("#")
      ? line.trim()
      : line
          .replace(/\/\/.*$/, "")
          .replace(/\s+/g, " ")
          .replace(/\s*([=,;{}()*/<>!&|?:])\s*/g, "$1")
          .trim()
  return {
    name: "orion:glsl-text",
    enforce: "pre",
    transform(code, id) {
      if (!id.endsWith("/src/core/shaders.ts")) return
      const tidied = code.replace(
        /\/\* glsl \*\/ `([\s\S]*?)`/g,
        (_, body: string) => `\`${body.split("\n").map(tidy).filter(Boolean).join("\n")}\n\``,
      )
      return { code: tidied, map: null }
    },
  }
}

export default defineConfig({
  plugins: [paletteText(), glslText(), react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
      // three-render-objects (under react-force-graph-3d) imports three's WebGPU renderer only to offer it; the map
      // draws with WebGL, so a stub keeps about 500 kB out of the memory page
      "three/webgpu": fileURLToPath(new URL("./src/pages/memory/no-webgpu.ts", import.meta.url)),
    },
  },
  build: {
    // FastAPI serves this folder; it is committed, so installing Orion needs no Node.
    outDir: "../orion/hud/dist",
    emptyOutDir: true,
    // three is the memory map's chunk, the biggest there is
    chunkSizeWarningLimit: 1600,
    rolldownOptions: {
      output: {
        // a shared chunk is named after one of its modules: three's keeps three's name, whatever small module joins it
        chunkFileNames: (chunk) =>
          `assets/${chunk.moduleIds.some((id) => id.includes("/node_modules/three/build/")) ? "three" : "[name]"}-[hash].js`,
      },
    },
  },
  worker: {
    format: "es",
    plugins: () => [paletteText(), glslText()],
    // the Core's worker drops console messages (the page keeps its own)
    rolldownOptions: { output: { minify: { compress: { dropConsole: true }, mangle: true, codegen: true } } },
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      // the object form: Vite's string shorthand would switch changeOrigin on, and Orion would refuse every POST
      "/api": { target: orion },
      "/mcp": { target: orion },
      "/ws": { target: orion, ws: true },
    },
  },
})
