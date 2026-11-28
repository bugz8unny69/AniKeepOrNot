// Runs dashboard.js at load with a page that has exactly index.html's element IDs.
const fs = require('fs'), vm = require('vm');
const [script, html] = process.argv.slice(2, 4).map(p => fs.readFileSync(p, 'utf8'));
const pathname = process.argv[4] || '/';   // The page: /, /running, /recognized or /settings.
const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]));
const stub = () => new Proxy(function () {}, {
  get: (t, k) => k === Symbol.toPrimitive ? () => '' : k === 'then' ? undefined : k === Symbol.iterator ? function* () {} :
    (k === 'length' ? 0 : stub()),
  apply: () => stub(), construct: () => stub(), set: () => true,
});
const document = new Proxy({}, { get: (t, k) => k === 'getElementById' ? id => (ids.has(id) ? stub() : null)
  : k === 'querySelectorAll' ? () => [] : stub() });
const context = { document, window: stub(), location: { pathname, hash: '', search: '', href: 'https://x' + pathname, assign() {}, reload() {} },
  localStorage: stub(), sessionStorage: stub(), navigator: stub(), history: stub(), fetch: () => new Promise(() => {}),
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {}, console, URL, URLSearchParams,
  AbortController: class { constructor() { this.signal = stub(); } abort() {} }, DOMException: Error, Intl,
  matchMedia: () => stub(), requestAnimationFrame: () => 0, Event: class {}, CustomEvent: class {}, Node: stub(),
  HTMLElement: stub(), confirm: () => false, alert() {}, structuredClone: x => x };
context.window = new Proxy(context, { get: (t, k) => (k in t ? t[k] : stub()) });
vm.createContext(context);
try { vm.runInContext(script, context, { filename: 'dashboard.js' }); console.log('loaded: no error at load'); }
catch (error) { console.log('FAILED at load:', error.message, (error.stack.split('\n').find(l => l.includes('dashboard.js')) || '').trim()); process.exitCode = 1; }
