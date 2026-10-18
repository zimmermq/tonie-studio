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

async function page() {
  const upload = element('upload');
  upload.querySelectorAll = () => [];
  const elements = await runPage(['app.js', 'index.js'], {
    ids: ['loading', 'pick', 'pickcount', 'albums', 'picked', 'send', 'tickshown', 'untick'],
    prepared: { upload },
    pageData: { words: { 'upload.sending': 'sending {percent} %', 'upload.failed': 'failed',
                         'batch.picked': '{count}' }, slowPick: 3 },
    globals: { XMLHttpRequest: FakeRequest },
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

// Refused - no audio in it, the disk full: the server's own page says why.
{
  const page_ = await page();
  page_.upload.fire('submit', { preventDefault: () => {} });
  const request = FakeRequest.last;
  request.status = 507;
  request.responseText = '<html><div class="warn">Speicher voll</div></html>';
  request.onload();
  assert.equal(page_.document.written, request.responseText);
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
