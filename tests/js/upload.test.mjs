// Uploading a folder: the page says how far it is instead of doing nothing for minutes.
import assert from 'node:assert/strict';
import { element, runPage } from './page.mjs';

// A request that is answered when the test says so.
class FakeRequest {
  constructor() { FakeRequest.last = this; this.upload = {}; }
  open(method, url) { this.method = method; this.url = url; }
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

  // Done: on to where the server sent it, the batch page.
  request.status = 200;
  request.responseURL = 'http://localhost/batch?ids=u1';
  request.onload();
  assert.equal(page_.location.href, 'http://localhost/batch?ids=u1');
}

// A dropped connection says so and lets the person try again.
{
  const page_ = await page();
  page_.upload.fire('submit', { preventDefault: () => {} });
  FakeRequest.last.onerror();
  assert.equal(page_.picked.textContent, 'failed');
  assert.equal(page_.send.disabled, false);
}
