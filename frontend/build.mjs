import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';
import { readFile } from 'node:fs/promises';

const licenses = await Promise.all([
  '@material/web/LICENSE', 'lit/LICENSE', 'lit-element/LICENSE',
  'lit-html/LICENSE', '@lit/reactive-element/LICENSE', 'tslib/LICENSE.txt',
].map(async path => `${path}\n${await readFile(new URL('../node_modules/' + path, import.meta.url), 'utf8')}`));

// Bundle locally: clients need neither an npm CDN nor a Node.js runtime.
await build({
  absWorkingDir: fileURLToPath(new URL('../', import.meta.url)),
  entryPoints: ['frontend/material.js'],
  outfile: 'static/material.js',
  bundle: true,
  minify: true,
  format: 'iife',
  target: ['es2020'],
  legalComments: 'inline',
  // Keep full third-party licenses in the artifact distributed to browsers.
  banner: { js: '/*!\n' + licenses.join('\n\n').replaceAll('*/', '* /') + '\n*/' },
  logLevel: 'info',
});
