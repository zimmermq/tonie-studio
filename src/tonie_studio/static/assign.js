// The script of the assign page; see templates/assign.html.
const data = pageData();

const words = data.words;
// A blank sticker held against the box shows up in what the box reports.
// Waiting for that beats telling people to reload until it appears.
const known = new Set([...document.querySelectorAll('input[name=ruid]')].map(i => i.value));
// What this page already reloaded for once. Without it, a tag teddyCloud
// never files keeps triggering a reload for as long as the page is open.
const alreadyTried = new URLSearchParams(window.location.search).get('new');
const hint = document.getElementById('hint');

// The page is drawn again for a new sticker, so what was typed so far goes
// along in the address; the server fills the fields with it. Left out, a
// sticker held up after typing the name would wipe the name.
const KEPT_FIELDS = ['series', 'episode', 'category'];
function reloadFor(ruid) {
  const params = new URLSearchParams({new: ruid});
  for (const name of KEPT_FIELDS) {
    const input = document.querySelector(`[name=${name}]`);
    if (input) params.set(name, input.value);
  }
  window.location = `${window.location.pathname}?${params}`;
}

let leaving = false;
document.addEventListener('box', event => {
  if (leaving) return;
  const box = event.detail;
  // teddyCloud files most fresh stickers on their first read, and the box
  // then reports them as lying on it rather than as unknown. Either way, a
  // sticker this page's list does not hold arrived after the page was drawn.
  const fresh = [box.unknown_ruid, box.on_box ? box.ruid : null]
    .find(ruid => ruid && !known.has(ruid));
  if (fresh) {
    if (fresh === alreadyTried) {
      // Reloading again would not help: the list has been fetched since and
      // still does not hold it. Reloading anyway is an endless loop that
      // throws the page back to the top every few seconds.
      const code = document.createElement('code');
      code.textContent = fresh;
      hint.replaceChildren(words['assign.unknown_tag'] + ' ', code, ' ' + words['assign.unknown_tag_hint']);
    } else {
      hint.textContent = words['assign.new_tag'];
      leaving = true;
      reloadFor(fresh);
    }
  }
});
