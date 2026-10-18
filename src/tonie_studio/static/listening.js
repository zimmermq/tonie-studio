// The script of the listening page; see templates/listening.html.
// Every few seconds the page fetches its own next rendering and takes over
// what changed: the line saying since when it listens, and each row's
// figures. A row keeps its place, so the list's search, filter and order
// (list.js) stay as the person left them; a tonie new on the page shows on
// the next visit. Reloading instead would throw the page back to the top, and
// drawing rows in script would be a second rendering to keep in step with the
// template.
const REFRESH_MS = 5000;

window.every(REFRESH_MS, async () => {
  if (document.hidden) return true;
  const response = await ask(window.location.href, {cache: 'no-store'});
  if (!response.ok) return true;
  const next = new DOMParser().parseFromString(await response.text(), 'text/html');
  const live = document.getElementById('live');
  const nextLive = next.getElementById('live');
  if (live && nextLive && live.innerHTML !== nextLive.innerHTML) live.innerHTML = nextLive.innerHTML;
  for (const there of next.querySelectorAll('#lists [data-search]')) {
    const here = document.getElementById(there.id);
    if (!here) continue;
    Object.assign(here.dataset, there.dataset);
    if (here.innerHTML !== there.innerHTML) here.innerHTML = there.innerHTML;
  }
  return true;
});
