#!/usr/bin/env node
// Export the v1 DSA library and decision tree as JSON for the backend import
// (BE-09, `scripts/import_dsa.py --content <file>`).
//
// Meant to live in the Jolt repo as `scripts/export-dsa.mjs` behind
// `npm run export:dsa`; kept here until it is ported. It bundles the TS content
// modules with Jolt's own esbuild (the `@/` alias resolves to `src/`), imports
// the result and prints:
//
//   { schemaVersion: 1, library: LIBRARY, decisionTree: { root: TREE_ROOT, nodes: TREE },
//     animations: Object.keys(ANIMATIONS) }
//
// Usage (from anywhere):
//   node scripts/export_dsa_from_jolt.mjs <path-to-Jolt-repo> [--out file.json]
// Ported into Jolt, the repo path defaults to the script's parent directory.

import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const args = process.argv.slice(2);
const outIdx = args.indexOf('--out');
const out = outIdx >= 0 ? args[outIdx + 1] : null;
const positional = args.filter((a, i) => !a.startsWith('--') && (outIdx < 0 || i !== outIdx + 1));
const repo = resolve(positional[0] ?? join(dirname(fileURLToPath(import.meta.url)), '..'));
const content = join(repo, 'src', 'tracks', 'dsa', 'content');

const require = createRequire(join(repo, 'package.json'));
const esbuild = require('esbuild');

const entry = `
export { LIBRARY } from ${JSON.stringify(join(content, 'library.ts'))};
export { TREE, TREE_ROOT } from ${JSON.stringify(join(content, 'decisionTree.ts'))};
export { ANIMATIONS } from ${JSON.stringify(join(content, 'animations.ts'))};
`;
const dir = mkdtempSync(join(tmpdir(), 'jolt-export-dsa-'));
try {
  const bundle = join(dir, 'dsa-content.mjs');
  await esbuild.build({
    stdin: { contents: entry, resolveDir: repo, loader: 'ts' },
    bundle: true,
    format: 'esm',
    platform: 'node',
    outfile: bundle,
    alias: { '@': join(repo, 'src') },
    logLevel: 'error',
  });
  const mod = await import(pathToFileURL(bundle).href);
  const json = JSON.stringify(
    {
      schemaVersion: 1,
      library: mod.LIBRARY,
      decisionTree: { root: mod.TREE_ROOT, nodes: mod.TREE },
      animations: Object.keys(mod.ANIMATIONS),
    },
    null,
    2,
  );
  if (out) writeFileSync(out, json + '\n');
  else process.stdout.write(json + '\n');
} finally {
  rmSync(dir, { recursive: true, force: true });
}
