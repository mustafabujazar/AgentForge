/**
 * Bundler-free production build for AgentForge's dashboard.
 *
 * Why this exists: the DSH Windows sandbox forbids child processes that capture
 * stdio, which makes esbuild's service spawn fail with `spawn EPERM`. Vite has a
 * hard dependency on esbuild, so `vite build` cannot run in this environment.
 *
 * This script (run after `tsc -p tsconfig.build.json`) assembles `dist/` from
 * plain, standards-based static files — no child processes, no bundler:
 *
 *   dist/index.html
 *   dist/assets/styles.css
 *   dist/assets/modules/**        # ESM output from tsc
 *   dist/assets/vendor/react.min.js
 *   dist/assets/vendor/react-dom.min.js
 *   dist/assets/vendor/react.js            # ESM facade over window.React
 *   dist/assets/vendor/react-dom-client.js # ESM facade over window.ReactDOM
 *
 * The emitted ESM uses extensionless relative specifiers (normal for tsc), so we
 * rewrite those to `.js` and redirect the bare `react` / `react-dom/client`
 * imports to the vendor facade modules. React itself is loaded as UMD globals
 * first, which the JSX classic runtime relies on (`React.createElement`).
 */

import { copyFileSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { join, relative, sep } from 'node:path';

const root = process.cwd();
const dist = join(root, 'dist');
const modulesDir = join(dist, 'assets', 'modules');
const vendorDir = join(dist, 'assets', 'vendor');
const srcDir = join(root, 'src');

function fail(message) {
  console.error(`[build] ${message}`);
  process.exit(1);
}

function ensureDir(dir) {
  mkdirSync(dir, { recursive: true });
}

function walk(dir) {
  const entries = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    const info = statSync(full);
    if (info.isDirectory()) entries.push(...walk(full));
    else if (name.endsWith('.js')) entries.push(full);
  }
  return entries;
}

if (!existsSync(join(modulesDir, 'main.js'))) {
  fail('compiled entry missing — run `tsc -p tsconfig.build.json` first.');
}

ensureDir(vendorDir);
for (const stale of ['react.mjs', 'react-dom-client.mjs']) {
  rmSync(join(vendorDir, stale), { force: true });
}

// ---------------------------------------------------------------------------
// 1. Vendor React 18 UMD builds + ESM facades (no network, no bundler).
// ---------------------------------------------------------------------------
const vendorFiles = [
  ['node_modules/react/umd/react.production.min.js', 'react.min.js'],
  ['node_modules/react-dom/umd/react-dom.production.min.js', 'react-dom.min.js'],
];

for (const [from, to] of vendorFiles) {
  const source = join(root, from);
  if (!existsSync(source)) fail(`missing vendor file: ${from} (run npm install)`);
  copyFileSync(source, join(vendorDir, to));
}

const reactFacade = `// ESM facade over the React 18 UMD global (loaded before this module).
const React = window.React;
if (!React) throw new Error('AgentForge: React UMD global missing');

export default React;
export const {
  Children,
  Component,
  Fragment,
  Profiler,
  PureComponent,
  StrictMode,
  Suspense,
  cloneElement,
  createContext,
  createElement,
  createFactory,
  createRef,
  forwardRef,
  isValidElement,
  lazy,
  memo,
  startTransition,
  useCallback,
  useContext,
  useDebugValue,
  useDeferredValue,
  useEffect,
  useId,
  useImperativeHandle,
  useInsertionEffect,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  useSyncExternalStore,
  useTransition,
  version,
} = React;
`;

const reactDomFacade = `// ESM facade over the ReactDOM 18 UMD global (loaded before this module).
const ReactDOM = window.ReactDOM;
if (!ReactDOM) throw new Error('AgentForge: ReactDOM UMD global missing');

export const createRoot = ReactDOM.createRoot;
export const hydrateRoot = ReactDOM.hydrateRoot;
export default ReactDOM;
`;

writeFileSync(join(vendorDir, 'react.js'), reactFacade);
writeFileSync(join(vendorDir, 'react-dom-client.js'), reactDomFacade);

// ---------------------------------------------------------------------------
// 2. Rewrite tsc ESM output for direct browser loading.
// ---------------------------------------------------------------------------
const RELATIVE_IMPORT_RE = /(from\s*['"])(\.{1,2}\/[^'"]+)(['"])/g;
const SIDE_EFFECT_IMPORT_RE = /(^|\n)\s*import\s*['"][^'"]+\.css['"];?/g;

let rewritten = 0;
for (const file of walk(modulesDir)) {
  let code = readFileSync(file, 'utf8');
  const original = code;

  // Source-level CSS imports are handled by the index.html <link> instead.
  code = code.replace(SIDE_EFFECT_IMPORT_RE, '$1');

  // Bare specifiers → vendor facades.
  code = code
    .replace(/(from\s*['"])react\/jsx-runtime(['"])/g, '$1/assets/vendor/react.js$2')
    .replace(/(from\s*['"])react-dom\/client(['"])/g, '$1/assets/vendor/react-dom-client.js$2')
    .replace(/(from\s*['"])react(['"])/g, '$1/assets/vendor/react.js$2');

  // Extensionless relative specifiers → .js (browsers require extensions).
  code = code.replace(RELATIVE_IMPORT_RE, (match, prefix, specifier, suffix) => {
    if (/\.(js|mjs|json|css)$/.test(specifier)) return match;
    return `${prefix}${specifier}.js${suffix}`;
  });

  if (code !== original) {
    writeFileSync(file, code);
    rewritten += 1;
  }
}

// ---------------------------------------------------------------------------
// 3. Styles + index.html.
// ---------------------------------------------------------------------------
copyFileSync(join(srcDir, 'styles.css'), join(dist, 'assets', 'styles.css'));

const indexHtml = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta name="theme-color" content="#070b11" />
    <meta
      name="description"
      content="AgentForge — mission control for autonomous multi-agent business operations."
    />
    <link
      rel="icon"
      href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%23070b11'/%3E%3Cpath d='M16 5l9 5.2v11.6L16 27l-9-5.2V10.2z' fill='none' stroke='%2347e0c0' stroke-width='1.6'/%3E%3Ccircle cx='16' cy='16' r='3.2' fill='%2347e0c0'/%3E%3C/svg%3E"
    />
    <link rel="stylesheet" href="/assets/styles.css" />
    <title>AgentForge · Mission Control</title>
  </head>
  <body>
    <div id="root"></div>
    <!-- React 18 UMD builds provide window.React / window.ReactDOM. -->
    <script src="/assets/vendor/react.min.js"></script>
    <script src="/assets/vendor/react-dom.min.js"></script>
    <!-- tsc-compiled application modules (native ESM, no bundler). -->
    <script type="module" src="/assets/modules/main.js"></script>
  </body>
</html>
`;

writeFileSync(join(dist, 'index.html'), indexHtml);

// ---------------------------------------------------------------------------
// 4. Summary + sanity assertions.
// ---------------------------------------------------------------------------
const moduleFiles = walk(modulesDir);
const totalBytes = moduleFiles.reduce((sum, file) => sum + statSync(file).size, 0);
const required = [join(dist, 'index.html'), join(dist, 'assets', 'styles.css'), join(modulesDir, 'main.js')];
for (const file of required) {
  if (!existsSync(file)) fail(`missing build artifact: ${relative(root, file)}`);
}

console.log('[build] bundler-free build complete');
console.log(`[build] dist/index.html`);
console.log(`[build] dist/assets/styles.css (${statSync(join(dist, 'assets', 'styles.css')).size} bytes)`);
console.log(`[build] dist/assets/modules/ — ${moduleFiles.length} ES modules (${totalBytes} bytes, ${rewritten} rewritten)`);
console.log('[build] dist/assets/vendor/ — react 18 UMD + ESM facades');

