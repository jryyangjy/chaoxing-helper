/* Read-only loading signals. Signatures stay in memory, never export content. */
() => {
  const selectors = ['video', '.TiMu', '.questionLi', '.mark_item', '.ans-attach-ct',
    'iframe[src*="knowledge"]', 'iframe[src*="work"]', '.vjs-tech', '.ans-job-icon',
    '.ans-video-js', '.video-js', '.ans-attach-online', '.attachment', '.ans-attach'];
  const counts = Object.fromEntries(selectors.map(s => [s,document.querySelectorAll(s).length]));
  const nodes = [...new Set(selectors.flatMap(s => [...document.querySelectorAll(s)]))].slice(0,120);
  // Text/source changes in an existing iframe (same URL) must count as navigation.
  // Never store raw iframe/video URLs or course question text in diagnostic files.
  const sample = nodes.map(el => {
    const name = el.tagName.toLowerCase();
    const src = el.getAttribute('src') || '';
    return name + '|' + src.slice(0,1800) + '|' + (el.textContent || '').slice(0,3000);
  }).join('\u241E').slice(0,50000);
  const hash = text => {
    let h = 2166136261;
    for (let i=0; i<text.length; ++i) {
      h = Math.imul(h ^ text.charCodeAt(i), 16777619);
    }
    return (h>>>0).toString(16);
  };
  const bodyText = (document.body?.innerText || '').slice(0,6000);
  const content = nodes.length > 0;
  const login = !!document.querySelector('input[type="password"]') &&
    !!document.querySelector('form, button[type="submit"]');
  return {ready:document.readyState, name:(document.title || '').slice(0,100),
    counts, content, contentSig:content ? hash(sample) : '',
    bodySig:hash(bodyText), bodyLength:bodyText.length, login};
}
