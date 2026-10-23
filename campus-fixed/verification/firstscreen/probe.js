// ASCII-only probe used by measure.ps1 (args through PowerShell 5.1 must stay ASCII).
// Reads the Today screen the way a first-time student sees it: filter chips and their counts,
// the cards in the default view, where the first card's deadline lands, and any touch target
// under 40x40 or horizontal overflow.
(() => {
  const de = document.documentElement;
  const cards = Array.from(document.querySelectorAll('.event-card')).filter((c) => c.getBoundingClientRect().height > 0);
  const first = cards[0];
  const dl = first ? first.querySelector('.deadline') : null;
  const small = [];
  document.querySelectorAll('button,a,input,select,summary').forEach((el) => {
    const r = el.getBoundingClientRect();
    if (r.height > 0 && r.width > 0 && (r.height < 40 || r.width < 40)) small.push({ tag: el.tagName.toLowerCase(), w: Math.round(r.width), h: Math.round(r.height) });
  });
  const empty = document.querySelector('.empty');
  return JSON.stringify({
    port: location.port,
    nav: (document.querySelector('nav [aria-current=page]') || {}).textContent || null,
    vw: innerWidth,
    vh: innerHeight,
    scrollW: de.scrollWidth,
    hScroll: de.scrollWidth > innerWidth,
    chips: Array.from(document.querySelectorAll('.filter-row button')).map((b) => {
      const r = b.getBoundingClientRect();
      return { t: b.textContent.trim(), pressed: b.getAttribute('aria-pressed'), count: (b.querySelector('.chip-count') || {}).textContent || null, cls: b.className, w: Math.round(r.width), h: Math.round(r.height) };
    }),
    cards: cards.length,
    firstCard: first ? first.querySelector('.card-title').textContent.slice(0, 34) : null,
    firstDeadline: dl ? dl.textContent.trim().slice(0, 40) : null,
    deadlineTop: dl ? Math.round(dl.getBoundingClientRect().top) : null,
    deadlineInFirstScreen: dl ? dl.getBoundingClientRect().top < innerHeight : null,
    emptyTitle: empty ? (empty.querySelector('h2') || {}).textContent : null,
    emptyText: empty ? (empty.querySelector('p') || {}).textContent : null,
    emptyAction: empty ? ((empty.querySelector('button') || {}).textContent || null) : null,
    smallCount: small.length,
    small: small.slice(0, 5)
  });
})()