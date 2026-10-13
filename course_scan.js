/* Course catalog adapter for mooc2-ans/studentcourse and legacy DOM.
 * Read-only except for a temporary data-study-helper-chapter attribute used by Playwright.
 * Never read hidden input tokens/cookies or mutate course completion.
 */
() => {
  const unique = (xs) => [...new Set(xs)];
  const isVisible = el => !!(el.getClientRects().length || el.offsetWidth || el.offsetHeight);
  const text = el => (el?.innerText || el?.textContent || el?.getAttribute('title') || '')
    .replace(/\s+/g, ' ').trim();
  const mooc2 = [...document.querySelectorAll('.chapter_item')];
  const list = [];
  if (mooc2.length) {
    for (const item of mooc2) {
      const control = item.querySelector('a.clicktitle, .clicktitle, a[href], [onclick]') ||
        (isVisible(item) ? item : null);
      if (!control || !isVisible(control)) continue;
      const number = text(item.querySelector('.catalog_sbar'));
      const title = text(control) || text(item.querySelector('.catalog_name'));
      const label = title.startsWith(number) || !number ? title : `${number} ${title}`;
      if (!label || label.length > 180) continue;
      const state = item.querySelector('.catalog_task') || item;
      // Already completed means the catalog *claims* completion; don't infer backend status from a video element.
      const complete = !!state.querySelector('.icon_yiwanc');
      const pending = text(state.querySelector('.catalog_points_yi'));
      const status = complete ? 'complete' : pending ? 'pending' : 'unknown';
      const identity = item.id || item.getAttribute('data-chapter-id') || item.getAttribute('data-knowledge-id') || '';
      list.push({el:control, label, status, identity, pending});
    }
  } else {
    const selectors = [
      '.chapter-item > a', '.posCatalog_name', '.posCatalog_title', '.ncells',
      'a[href*="chapterId"]', 'a[href*="knowledge"]',
      '[onclick*="toOldKnowledge"]', '[onclick*="toKnowledge"]',
      '[data-chapter-id]', '[data-knowledge-id]',
      '.catalog_name a.clicktitle', '.chapter_unit a.clicktitle'
    ];
    const raw = unique([...document.querySelectorAll(selectors.join(','))]);
    const leaf = raw.filter(el => !raw.some(other => other !== el && el.contains(other)));
    for (const el of leaf) {
      if (!isVisible(el)) continue;
      const label = text(el);
      if (label.length < 2 || label.length > 180) continue;
      list.push({el, label, status:'unknown', identity:el.id || '', pending:''});
    }
  }
  const seen = new Set();
  return list.flatMap(entry => {
    const href = entry.el.getAttribute('href') || '';
    const onclick = entry.el.getAttribute('onclick') || '';
    const signature = [entry.identity, entry.label, href.slice(0,150), onclick.slice(0,150)].join('\u241F');
    if (seen.has(signature)) return [];
    seen.add(signature);
    const index = seen.size - 1;
    entry.el.setAttribute('data-study-helper-chapter', String(index));
    return [{index, signature, label:entry.label, status:entry.status,
      pending:entry.pending.slice(0,30), identity:entry.identity}];
  }).slice(0,300);
}
