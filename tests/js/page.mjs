// Runs a page's real scripts against a minimal stand-in for the page, so what
// they do can be checked without a browser. Only what the scripts touch is
// there: elements by id, their text, class, style and hidden state. The
// result also carries ``timers``, the steps the scripts scheduled for later,
// ``fetched``, every request they made, ``dispatch``, to fire an event, and
// ``window``, to reach what the scripts put there.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const STATIC = fileURLToPath(new URL('../../src/tonie_studio/static/', import.meta.url));

// A stand-in element. ``children`` maps a selector to what querySelector
// answers inside it; ``fire`` hands an event to its listeners.
export function element(id, text = '', { dataset = {}, children = {} } = {}) {
  const listeners = {};
  return {
    id, textContent: text, hidden: false, className: '', style: {}, dataset,
    checked: false, disabled: false,
    classList: { toggle() {}, add() {}, remove() {} },
    addEventListener(type, listener) { (listeners[type] ||= []).push(listener); },
    fire(type, event = {}) { for (const listener of listeners[type] || []) listener(event); },
    querySelector: selector => children[selector] || null,
    querySelectorAll() { return []; },
  };
}

// ``ids`` are the elements the page has; ``pageData`` is what the server puts
// in #page-data; ``answers`` maps a fetched URL to the JSON it answers with;
// ``queries`` maps a selector to the elements document.querySelectorAll finds;
// ``texts`` gives elements by id with their content, such as other JSON blocks;
// ``prepared`` gives elements by id built with ``element``, children and all;
// ``found`` maps a selector to what document.querySelector answers;
// ``globals`` adds what else a script uses of the browser, such as DOMParser.
export async function runPage(scripts, { ids = [], pageData = {}, answers = {},
                                         queries = {}, texts = {}, prepared = {},
                                         found = {}, globals = {} } = {}) {
  const elements = Object.fromEntries(ids.map(id => [id, element(id)]));
  for (const [id, text] of Object.entries(texts)) elements[id] = element(id, text);
  Object.assign(elements, prepared);
  elements['page-data'] = element('page-data', JSON.stringify(pageData));
  // Every step a script schedules for later; a test reads whether it goes on.
  const timers = [];
  // Listeners on the document, by event type, and every request made.
  const listeners = {};
  const fetched = [];
  const document = {
    getElementById: id => elements[id] || null,
    querySelector: selector => found[selector] || null,
    querySelectorAll: selector => queries[selector] || [],
    addEventListener(type, listener) { (listeners[type] ||= []).push(listener); },
    dispatchEvent() {},
  };
  class CustomEvent {
    constructor(type, init = {}) { this.type = type; this.detail = init.detail; }
  }
  class FormData {
    constructor() { this.fields = {}; }
    append(name, value) { this.fields[name] = value; }
  }
  const context = {
    document, console, JSON, Math, Promise, URLSearchParams, FormData, CustomEvent,
    setTimeout: step => timers.push(step),
    fetch: async (url, options = {}) => {
      fetched.push({ url, method: options.method || 'GET', body: options.body });
      return { ok: url in answers, json: async () => answers[url],
               text: async () => String(answers[url]) };
    },
  };
  context.window = context;
  context.addEventListener = () => {};
  // A page that reloads itself only counts here.
  context.location = { href: 'http://localhost/page', reloaded: 0,
                       reload() { this.reloaded += 1; } };
  // A touch screen: hover-only extras such as the cover preview stay off.
  context.matchMedia = () => ({ matches: false });
  Object.assign(context, globals);
  vm.createContext(context);
  for (const name of scripts) {
    vm.runInContext(readFileSync(STATIC + name, 'utf8'), context, { filename: name });
  }
  // Let the page's first round of fetching and showing finish.
  for (let round = 0; round < 10; round++) await new Promise(done => setImmediate(done));
  // Hands ``detail`` to the page's listeners for ``type`` and lets them finish;
  // ``event`` adds to what they are handed, such as its target.
  async function dispatch(type, detail, event = {}) {
    for (const listener of listeners[type] || []) await listener({ detail, ...event });
    for (let round = 0; round < 10; round++) await new Promise(done => setImmediate(done));
  }
  return Object.assign(elements, { timers, fetched, dispatch, location: context.location,
                                   window: context });
}
