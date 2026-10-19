// Uploading a folder: the page says how far it is instead of doing nothing for minutes.
import assert from 'node:assert/strict';
import { element, runPage } from './page.mjs';

// A request that is answered when the test says so.
class FakeRequest {
  constructor() { FakeRequest.last = this; this.upload = {}; this.headers = {}; }
  open(method, url) { this.method = method; this.url = url; }
  setRequestHeader(name, value) { this.headers[name] = value; }
  send(body) { this.body = body; }
}

// Reads a refusal page as a browser would, as far as the page looks at it:
// the message is the text of ``main > .warn``.
class FakeParser {
  parseFromString(text) {
    const match = /<main><div class="warn">([^<]*)<\/div>/.exec(text);
    return { querySelector: selector => (selector === 'main > .warn' && match
      ? { textContent: match[1] } : null) };
  }
}

async function page() {
  const upload = element('upload');
  upload.querySelectorAll = () => [];
  const elements = await runPage(['app.js', 'index.js'], {
    ids: ['loading', 'pick', 'pickcount', 'albums', 'picked', 'send', 'tickshown', 'untick',
          'refusal'],
    prepared: { upload },
    pageData: { words: { 'upload.sending': 'sending {percent} %', 'upload.failed': 'failed',
                         'batch.picked': '{count}' }, slowPick: 3 },
    globals: { XMLHttpRequest: FakeRequest, DOMParser: FakeParser },
  });
  return elements;
}

{
  const page_ = await page();
  let stopped = false;
  page_.upload.fire('submit', { preventDefault: () => { stopped = true; } });
  const request = FakeRequest.last;

  assert.equal(stopped, true);
  assert.equal(request.method, 'POST');
  assert.equal(page_.send.disabled, true);

  request.upload.onprogress({ lengthComputable: true, loaded: 42, total: 100 });
  assert.equal(page_.picked.textContent, 'sending 42 %');

  // Done: the server says where to go, and the page goes there once.
  assert.equal(request.headers['X-Upload'], '1');
  request.status = 200;
  request.responseText = JSON.stringify({ next: '/batch?ids=u1' });
  request.onload();
  assert.equal(page_.location.href, '/batch?ids=u1');
}

// Refused - no audio in it, the disk full: the server's page says why, and
// its message is shown on the album list. Written over this page instead, it
// would run app.js a second time (whose top-level names then clash) and push
// no history entry, so "back" would leave the album list as well.
{
  const page_ = await page();
  page_.refusal.hidden = true;
  page_.upload.fire('submit', { preventDefault: () => {} });
  const request = FakeRequest.last;
  request.status = 507;
  request.responseText = '<html><body><main><div class="warn">Speicher voll</div></main></body></html>';
  request.onload();
  assert.equal(page_.document.written, null);
  assert.equal(page_.refusal.textContent, 'Speicher voll');
  assert.equal(page_.refusal.hidden, false);
  assert.equal(page_.send.disabled, false);
  assert.equal(page_.window.onbeforeunload, null);

  // The next try starts without the old refusal in view.
  page_.upload.fire('submit', { preventDefault: () => {} });
  assert.equal(page_.refusal.hidden, true);
}

// A refusal that is not a page of this app still says the upload failed.
{
  const page_ = await page();
  page_.upload.fire('submit', { preventDefault: () => {} });
  const request = FakeRequest.last;
  request.status = 413;
  request.responseText = 'Request Entity Too Large';
  request.onload();
  assert.equal(page_.document.written, null);
  assert.equal(page_.refusal.textContent, 'failed');
  assert.equal(page_.refusal.hidden, false);
}

// While it is being sent, leaving the page is asked about; not afterwards.
{
  const page_ = await page();
  page_.upload.fire('submit', { preventDefault: () => {} });
  assert.equal(typeof page_.window.onbeforeunload, 'function');
  FakeRequest.last.onerror();
  assert.equal(page_.window.onbeforeunload, null);
}

// A dropped connection says so and lets the person try again.
{
  const page_ = await page();
  page_.upload.fire('submit', { preventDefault: () => {} });
  FakeRequest.last.onerror();
  assert.equal(page_.picked.textContent, 'failed');
  assert.equal(page_.send.disabled, false);
}
