// The script of the assign page; see templates/assign.html.
const data = pageData();

const words = data.words;
// A blank sticker reaches teddyCloud as a tag it does not know, which is
// exactly what the box reports when one is held against it. Waiting for that
// beats telling people to reload until it appears.
const known = new Set([...document.querySelectorAll('input[name=ruid]')].map(i => i.value));
// What this page already reloaded for once. Without it, a tag teddyCloud
// never files keeps triggering a reload for as long as the page is open.
const alreadyTried = new URLSearchParams(window.location.search).get('new');
const hint = document.getElementById('hint');

let leaving = false;
document.addEventListener('box', event => {
  if (leaving) return;
  const fresh = event.detail.unknown_ruid;
  if (fresh && !known.has(fresh)) {
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
      window.location = `${window.location.pathname}?new=${encodeURIComponent(fresh)}`;
    }
  }
});
