// The script of the listening page; see templates/listening.html.
// The page fetches its own next rendering and swaps in only the parts that
// changed. Reloading instead would throw the page back to the top every few
// seconds, and drawing the rows in script would be a second rendering to
// keep in step with the template.
(() => {
  const parts = ['live', 'lists'];
  async function refresh() {
    if (document.hidden) return;
    try {
      const response = await fetch(window.location.href, {cache: 'no-store'});
      if (!response.ok) return;
      const next = new DOMParser().parseFromString(await response.text(), 'text/html');
      for (const id of parts) {
        const here = document.getElementById(id);
        const there = next.getElementById(id);
        if (here && there && here.innerHTML !== there.innerHTML) here.innerHTML = there.innerHTML;
      }
    } catch (error) {
      // A missed refresh is caught up by the next one.
    }
  }
  setInterval(refresh, 5000);
  document.addEventListener('visibilitychange', refresh);
})();
