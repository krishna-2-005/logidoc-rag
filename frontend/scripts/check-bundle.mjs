// Checks the built bundle against its size budget (npm run size, after npm run build).
//
// First load: the scripts index.html itself names (the entry and its preloaded chunks), gzipped.
// Every page and the charts are separate chunks fetched when a route needs them (src/App.tsx).
// Fails when the first load or any single chunk exceeds its budget, so a change that pulls a heavy
// dependency back into the first load is seen in CI. No dependency: node's own zlib.

import { readFileSync, readdirSync } from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { gzipSync } from "node:zlib"

const FIRST_LOAD_GZIP_KB = 150
const CHUNK_KB = 500

const dist = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "dist")
const html = readFileSync(path.join(dist, "index.html"), "utf8")
const kb = (bytes) => bytes / 1000

const first = [...html.matchAll(/(?:src|href)="\/(assets\/[^"]+\.js)"/g)].map((match) => match[1])
if (first.length === 0) {
  console.error("No script found in dist/index.html: run npm run build first.")
  process.exit(2)
}

const firstGzip = first.reduce((total, file) => total + gzipSync(readFileSync(path.join(dist, file))).length, 0)
const chunks = readdirSync(path.join(dist, "assets"))
  .filter((name) => name.endsWith(".js"))
  .map((name) => ({ name, size: readFileSync(path.join(dist, "assets", name)).length }))
  .sort((a, b) => b.size - a.size)
const total = chunks.reduce((sum, chunk) => sum + chunk.size, 0)

console.log(`First load: ${first.length} file(s), ${kb(firstGzip).toFixed(1)} kB gzip (budget ${FIRST_LOAD_GZIP_KB} kB)`)
console.log(`Largest chunk: ${chunks[0].name}, ${kb(chunks[0].size).toFixed(1)} kB (budget ${CHUNK_KB} kB)`)
console.log(`All JavaScript: ${chunks.length} chunks, ${kb(total).toFixed(1)} kB`)

const problems = []
if (kb(firstGzip) > FIRST_LOAD_GZIP_KB) problems.push(`the first load is ${kb(firstGzip).toFixed(1)} kB gzip, over ${FIRST_LOAD_GZIP_KB} kB`)
for (const chunk of chunks) {
  if (kb(chunk.size) > CHUNK_KB) problems.push(`${chunk.name} is ${kb(chunk.size).toFixed(1)} kB, over ${CHUNK_KB} kB`)
}

if (problems.length) {
  for (const problem of problems) console.error(`Bundle budget exceeded: ${problem}`)
  process.exit(1)
}
