// Preloaded with `node --require` for the SSR smoke test so that the
// JSX classic runtime's global `React.createElement` reference resolves.
globalThis.React = require('react');
