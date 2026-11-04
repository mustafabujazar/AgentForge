/**
 * Static verification of the bundler-free `dist/` output.
 * Run with: node scripts/verify-build.mjs
 *
 * Checks:
 *  1. index.html references only files that exist, in the right order.
 *  2. Every emitted ES module import resolves to a real file.
 *  3. No extensionless relative imports or unrewritten bare specifiers remain.
 *  4. Every named import from the React facade exists on the React 18 UMD build.
 *  5. The ReactDOM UMD build exposes createRoot.
 */

import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, resolve, sep } from 'node:path';
import vm from 'node:vm';

const root = process.cwd();
const dist = join(root, 'dist');
const modulesDir = join(dist, 'assets', 'modules');
const problems = [];

function walk(dir, filter) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) out.push(...walk(full, filter));
    else if (filter(name)) out.push(full);
  }
  return out;
}

/* -------------------------------------------------------------------------- */
/* 1. index.html                                                              */
/* -------------------------------------------------------------------------- */
const indexPath = join(dist, 'index.html');
if (!existsSync(indexPath)) problems.push('dist/index.html is missing');
const indexHtml = existsSync(indexPath) ? readFileSync(indexPath, 'utf8') : '';

const localRefs = [...indexHtml.matchAll(/(?:src|href)="(\/assets\/[^"]+)"/g)].map((m) => m[1]);
if (localRefs.length === 0) problems.push('index.html has no /assets references');
for (const ref of localRefs) {
  const file = join(dist, ref.replace(/^\//, '').split('/').join(sep));
  if (!existsSync(file)) problems.push(`index.html references missing file: ${ref}`);
}

const reactScript = indexHtml.indexOf('/assets/vendor/react.min.js');
const reactDomScript = indexHtml.indexOf('/assets/vendor/react-dom.min.js');
const appScript = indexHtml.indexOf('/assets/modules/main.js');
if (reactScript === -1 || reactDomScript === -1 || appScript === -1) {
  problems.push('index.html is missing react/react-dom/app script tags');
} else if (!(reactScript < reactDomScript && reactDomScript < appScript)) {
  problems.push('index.html script order must be react → react-dom → app');
}
if (!indexHtml.includes('type="module" src="/assets/modules/main.js"')) {
  problems.push('app entry must load as a module script');
}

/* -------------------------------------------------------------------------- */
/* 2–3. Module graph                                                          */
/* -------------------------------------------------------------------------- */
const moduleFiles = walk(modulesDir, (name) => name.endsWith('.js'));
if (moduleFiles.length < 25) problems.push(`expected 25+ modules, found ${moduleFiles.length}`);

const NAMED_IMPORT_RE = /import\s*(?:([\w$]+)\s*,?\s*)?(?:\{([^}]+)\})?\s*from\s*['"]([^'"]+)['"]/g;
const SIDE_EFFECT_RE = /import\s*['"]([^'"]+)['"]/g;

const reactFacadePath = join(dist, 'assets', 'vendor', 'react.js');
const reactNamedImports = new Set();
const reactDomNamedImports = new Set();

for (const file of moduleFiles) {
  const code = readFileSync(file, 'utf8');

  for (const match of code.matchAll(NAMED_IMPORT_RE)) {
    const [, , namedBlock, specifier] = match;
    if (specifier === '/assets/vendor/react.js' && namedBlock) {
      for (const part of namedBlock.split(',')) {
        const name = part.trim().split(/\s+as\s+/)[0]?.trim();
        if (name) reactNamedImports.add(name);
      }
    }
    if (specifier === '/assets/vendor/react-dom-client.js' && namedBlock) {
      for (const part of namedBlock.split(',')) {
        const name = part.trim().split(/\s+as\s+/)[0]?.trim();
        if (name) reactDomNamedImports.add(name);
      }
    }
    resolveImport(file, specifier);
  }

  for (const match of code.matchAll(SIDE_EFFECT_RE)) {
    const specifier = match[1];
    if (code.slice(match.index, match.index + 10).includes('from')) continue;
    resolveImport(file, specifier);
  }

  if (/import\s+[^;]*from\s*['"]\.\.?\//.test(code)) {
    // all relative specifiers must end with .js
    for (const match of code.matchAll(/from\s*['"](\.[^'"]+)['"]/g)) {
      if (!/\.(js|mjs|json|css)$/.test(match[1])) {
        problems.push(`${relativePath(file)} has extensionless import ${match[1]}`);
      }
    }
  }
  if (/\.css['"]/.test(code)) problems.push(`${relativePath(file)} still imports CSS`);
  if (/\bfrom\s*['"](react|react-dom\/client)['"]/.test(code)) {
    problems.push(`${relativePath(file)} still has a bare React specifier`);
  }
}

function resolveImport(fromFile, specifier) {
  if (specifier.startsWith('/')) {
    const target = join(dist, specifier.replace(/^\//, '').split('/').join(sep));
    if (!existsSync(target)) problems.push(`${relativePath(fromFile)} imports missing ${specifier}`);
    return;
  }
  if (!specifier.startsWith('.')) {
    problems.push(`${relativePath(fromFile)} imports bare specifier ${specifier}`);
    return;
  }
  const target = resolve(dirname(fromFile), specifier);
  if (!existsSync(target)) {
    problems.push(`${relativePath(fromFile)} imports missing relative ${specifier}`);
  }
}

function relativePath(file) {
  return file.replace(root + sep, '').replaceAll(sep, '/');
}

/* -------------------------------------------------------------------------- */
/* 4. React UMD global exports                                                */
/* -------------------------------------------------------------------------- */
function createSandbox() {
  const sandbox = { console };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.document = {
    createElement: () => ({ style: {}, setAttribute() {}, appendChild() {} }),
    documentElement: { style: {} },
    addEventListener() {},
    removeEventListener() {},
  };
  sandbox.navigator = { userAgent: 'node' };
  sandbox.setTimeout = setTimeout;
  sandbox.clearTimeout = clearTimeout;
  sandbox.setInterval = setInterval;
  sandbox.clearInterval = clearInterval;
  sandbox.queueMicrotask = queueMicrotask;
  sandbox.requestAnimationFrame = (cb) => setTimeout(cb, 0);
  sandbox.cancelAnimationFrame = () => {};
  sandbox.performance = { now: () => 0 };
  sandbox.MessageChannel = function MessageChannel() {
    const makePort = () => ({ onmessage: null, postMessage() {}, close() {}, addEventListener() {} });
    return { port1: makePort(), port2: makePort() };
  };
  vm.createContext(sandbox);
  return sandbox;
}

function runUmd(file, sandbox) {
  vm.runInContext(readFileSync(file, 'utf8'), sandbox, { filename: file });
}

let React = null;
let ReactDOM = null;
try {
  const sandbox = createSandbox();
  runUmd(join(dist, 'assets', 'vendor', 'react.min.js'), sandbox);
  React = sandbox.React ?? sandbox.window.React;
  runUmd(join(dist, 'assets', 'vendor', 'react-dom.min.js'), sandbox);
  ReactDOM = sandbox.ReactDOM ?? sandbox.window.ReactDOM;
} catch (error) {
  problems.push(`React UMD failed to evaluate: ${error.message}`);
}

if (!React) {
  problems.push('React UMD did not expose a React global');
} else {
  if (typeof React.createElement !== 'function') problems.push('React.createElement missing');
  for (const name of reactNamedImports) {
    if (!(name in React)) problems.push(`react.js facade exports missing React member: ${name}`);
  }
  console.log(`[verify] React UMD v${React.version}; ${reactNamedImports.size} named hooks/components used`);
}

if (!ReactDOM) {
  problems.push('ReactDOM UMD did not expose a ReactDOM global');
} else if (typeof ReactDOM.createRoot !== 'function') {
  problems.push('ReactDOM.createRoot missing');
} else {
  console.log('[verify] ReactDOM UMD exposes createRoot');
}

for (const name of reactDomNamedImports) {
  if (name !== 'createRoot' && name !== 'hydrateRoot') {
    problems.push(`react-dom-client facade exports unexpected member: ${name}`);
  }
}

/* -------------------------------------------------------------------------- */
/* 5. Vendor ESM facades match UMD exports                                    */
/* -------------------------------------------------------------------------- */
if (React) {
  const facade = readFileSync(reactFacadePath, 'utf8');
  const destructureMatch = /export const \{([\s\S]*?)\} = React;/.exec(facade);
  if (!destructureMatch) {
    problems.push('react.js facade does not destructure the React global');
  } else {
    for (const raw of destructureMatch[1].split(',')) {
      const name = raw.trim();
      if (name && !(name in React)) {
        problems.push(`react.js facade references missing React member: ${name}`);
      }
    }
  }
}

/* -------------------------------------------------------------------------- */
/* Result                                                                     */
/* -------------------------------------------------------------------------- */
if (problems.length > 0) {
  console.error('[verify] FAILED');
  for (const problem of problems) console.error(`  - ${problem}`);
  process.exit(1);
}

const totalBytes = walk(dist, () => true)
  .filter((file) => file.endsWith('.js') || file.endsWith('.css') || file.endsWith('.html') || file.endsWith('.mjs'))
  .reduce((sum, file) => sum + statSync(file).size, 0);

console.log(`[verify] OK — ${moduleFiles.length} modules, dist payload ${(totalBytes / 1024).toFixed(1)} KB`);

