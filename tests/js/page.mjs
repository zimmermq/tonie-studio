// Runs a page's real scripts against a minimal stand-in for the page, so what
// they do can be checked without a browser. Only what the scripts touch is
// there: elements by id, their text, class, style and hidden state.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const STATIC = fileURLToPath(new URL('../../src/tonie_studio/static/', import.meta.url));

function element(id, text = '') {
  return {
    id, textContent: text, hidden: false, className: '', style: {}, dataset: {},
    classList: { toggle() {}, add() {}, remove() {} },
    addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; },
  };
}

// ``ids`` are the elements the page has; ``pageData`` is what the server puts
// in #page-data; ``answers`` maps a fetched URL to the JSON it answers with.
export async function runPage(scripts, { ids = [], pageData = {}, answers = {} } = {}) {
  const elements = Object.fromEntries(ids.map(id => [id, element(id)]));
  elements['page-data'] = element('page-data', JSON.stringify(pageData));
  const pending = [];
  const document = {
    getElementById: id => elements[id] || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
    dispatchEvent() {},
  };
  const context = {
    document, console, JSON, Math, Promise, URLSearchParams,
    setTimeout: () => 0,
    fetch: async url => ({ ok: url in answers, json: async () => answers[url] }),
  };
  context.window = context;
  vm.createContext(context);
  for (const name of scripts) {
    vm.runInContext(readFileSync(STATIC + name, 'utf8'), context, { filename: name });
  }
  // Let the page's first round of fetching and showing finish.
  for (let round = 0; round < 10; round++) await new Promise(done => setImmediate(done));
  return elements;
}
