/*
 * Mobile acceptance probe. Injected into the real browser by
 * verification/mobile/measure.ps1 through `bsk evaluate`.
 *
 * Reads layout facts only, never mutates the page: viewport size, horizontal
 * overflow, touch targets, and what the first screen actually shows. All of it
 * comes from getBoundingClientRect / getComputedStyle in a real engine, not
 * from reading CSS by hand.
 *
 * ASCII-only on purpose: the transport is a Windows PowerShell 5.1 native
 * argument, which decodes a BOM-less file with the ANSI code page; non-ASCII
 * inside the script corrupts the code that follows it.
 */
(() => {
  const iw = innerWidth, ih = innerHeight, de = document.documentElement;
  const all = (sel) => Array.from(document.querySelectorAll(sel));
  const isSvg = (e) => typeof e.className !== 'string';
  const cls = (e) => (isSvg(e) ? '' : String(e.className || '').trim().split(/\s+/).slice(0, 3).join('.'));
  const label = (e) => (e.getAttribute('aria-label') || e.textContent || e.getAttribute('title') || e.value || '')
    .replace(/\s+/g, ' ').trim().slice(0, 34);
  const visible = (e) => {
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    if (parseFloat(s.opacity || '1') <= 0.05) return false;
    if (e.closest('[inert]')) return false;
    return r.width > 0 && r.height > 0;
  };
  const rect = (e) => {
    const r = e.getBoundingClientRect();
    return { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height), b: Math.round(r.bottom), r: Math.round(r.right) };
  };

  // 1) Horizontal overflow: any rendered element crossing the viewport edges.
  //    `clipped` names the nearest ancestor that scrolls/clips horizontally, so
  //    a chip row inside its own scroller is not mistaken for page overflow.
  const clippedBy = (e) => {
    let p = e.parentElement;
    while (p && p !== document.documentElement) {
      if (getComputedStyle(p).overflowX !== 'visible') return p.tagName.toLowerCase() + (cls(p) ? '.' + cls(p) : '');
      p = p.parentElement;
    }
    return null;
  };
  const overflow = [];
  for (const e of all('body *')) {
    if (!visible(e)) continue;
    const r = e.getBoundingClientRect();
    if (r.right > iw + 0.5 || r.left < -0.5) {
      overflow.push({ sel: e.tagName.toLowerCase() + (cls(e) ? '.' + cls(e) : ''), left: Math.round(r.left), right: Math.round(r.right), w: Math.round(r.width), text: label(e), clipped: clippedBy(e) });
    }
  }

  // 2) Touch targets: the EFFECTIVE hit area is smaller than 40x40 CSS px.
  //    A checkbox/radio is activated through the <label> that wraps it, so the
  //    label box is what a thumb has to hit (WCAG 2.5.8 target size).
  const small = [], seen = new Set();
  for (const raw of all('button,a[href],input:not([type=hidden]),select,textarea,summary,[role=button],label.upload-button,label.check')) {
    if (!visible(raw)) continue;
    const e = (raw.matches('input[type=checkbox],input[type=radio]') && raw.closest('label')) || raw;
    if (!visible(e) || seen.has(e)) continue;
    seen.add(e);
    const r = e.getBoundingClientRect();
    if (r.width < 39.5 || r.height < 39.5) small.push({ tag: raw.tagName.toLowerCase(), cls: cls(raw), target: e === raw ? 'self' : 'label', w: Math.round(r.width), h: Math.round(r.height), text: label(raw) });
  }

  // 3) First screen: is an item with a deadline visible without scrolling?
  const deadlines = all('.deadline').filter(visible);
  const aboveFold = (e) => { const r = e.getBoundingClientRect(); return r.top < ih * 0.5 && r.bottom > 0; };
  const onFirstScreen = (e) => { const r = e.getBoundingClientRect(); return r.top >= 0 && r.top < ih; };
  const firstDeadlineInFirstHalf = deadlines.find(aboveFold) || null;
  const firstDeadlineOnFirstScreen = deadlines.find(onFirstScreen) || null;
  const cards = all('.event-card,.personal-card').filter(visible);
  const cardsAboveHalf = cards.filter(aboveFold).length;
  const firstVisibleCard = cards[0] || null;

  // 4) Page identity, so a measurement can be tied to the page it came from.
  //    navIndex is 1-based inside the main navigation (0 = no active entry);
  //    it is ASCII-safe for shell comparisons.
  const heading = (document.querySelector('main h1, h1') || {}).textContent || '';
  const activeNav = (document.querySelector('nav [aria-current=page]') || {}).textContent || '';
  const navButtons = all('nav[aria-label] button');
  const navIndex = navButtons.findIndex((b) => b.getAttribute('aria-current') === 'page') + 1;

  return JSON.stringify({
    url: location.pathname + location.search,
    iw, ih, dpr: devicePixelRatio,
    touch: navigator.maxTouchPoints,
    hoverNone: matchMedia('(hover: none)').matches,
    sw: de.scrollWidth, bsw: document.body.scrollWidth, cw: de.clientWidth,
    hScroll: de.scrollWidth > de.clientWidth || document.body.scrollWidth > de.clientWidth,
    overflowCount: overflow.length, overflow: overflow.slice(0, 14),
    smallCount: small.length, small: small.slice(0, 26),
    heading: heading.replace(/\s+/g, ' ').trim().slice(0, 40),
    activeNav: activeNav.replace(/\s+/g, ' ').trim().slice(0, 20),
    navIndex,
    cardsAboveHalf,
    firstCardTop: firstVisibleCard ? Math.round(firstVisibleCard.getBoundingClientRect().top) : null,
    firstCardTitle: firstVisibleCard ? label(firstVisibleCard.querySelector('.card-title,h3') || firstVisibleCard) : null,
    firstDeadlineInFirstHalf: !!firstDeadlineInFirstHalf,
    firstDeadlineOnFirstScreen: !!firstDeadlineOnFirstScreen,
    firstDeadlineTop: firstDeadlineOnFirstScreen ? Math.round(firstDeadlineOnFirstScreen.getBoundingClientRect().top) : (deadlines[0] ? Math.round(deadlines[0].getBoundingClientRect().top) : null),
    deadlineSample: deadlines.slice(0, 3).map((d) => Object.assign(rect(d), { text: label(d) })),
  });
})()