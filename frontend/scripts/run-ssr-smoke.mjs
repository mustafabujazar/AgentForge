// Runs the SSR smoke test after `tsc -p tsconfig.ssr.json`.
// Uses CommonJS require (no child process, which the sandbox restricts).
import { createRequire } from 'node:module';
import { writeFileSync } from 'node:fs';

const require = createRequire(import.meta.url);

// Compiled output is CommonJS; package.json type:module would otherwise make
// Node treat .js files as ESM.
writeFileSync(new URL('../.ssr-test/package.json', import.meta.url), '{"type":"commonjs"}\n');

// React's classic JSX runtime references the global React identifier.
globalThis.React = require('react');

require('../.ssr-test/scripts/ssr-smoke.js');
