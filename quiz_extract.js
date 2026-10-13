/* Page-local DOM adapter. No network requests, no grades/progress manipulation. */
() => {
  const normalize = s => (s || '').replace(/\u00a0/g, ' ').replace(/[ \t]+/g, ' ').trim();
  const rootsSelector = '.TiMu.newTiMu, .TiMu, .questionLi, .mark_item, .question-item, .questionItem, [data-question-id]';
  const all = [...document.querySelectorAll(rootsSelector)];
  // Prefer leaf question elements over enclosing question lists; suppress duplicate parent hits.
  let roots = all.filter(el => !el.querySelector(rootsSelector));
  if (!roots.length) {
    const anchors = [...document.querySelectorAll('.singleQuesId[data], .singleQuesId')];
    roots = anchors.map(el => el.closest('li, .clearfix, .subject, .question') || el.parentElement).filter(Boolean);
  }
  const seen = new Set();
  roots = roots.filter(el => { if (seen.has(el)) return false; seen.add(el); return true; });
  const results = [];
  for (const root of roots.slice(0, 200)) {
    const full = normalize(root.innerText || root.textContent || '');
    if (!full || full.length < 5) continue;
    const titleNode = root.querySelector('.Zy_TItle, .mark_name, .question-title, .subject-title, .q_title, .questionName, .stem, .TiMu_title');
    const question = normalize(titleNode?.innerText || titleNode?.textContent || full.split(/\n/).slice(0, 3).join(' ')).slice(0, 1600);
    const first = full.slice(0, 120);
    let type = /多选|多项选择/.test(first) ? 'multiple' : /单选|单项选择/.test(first) ? 'single' : /判断|对错|是非/.test(first) ? 'true_false' : /填空/.test(first) ? 'blank' : 'unknown';
    const opts = new Map();
    const nodes = root.querySelectorAll('.answerList li, .answer_list li, .answerList label, .Zy_ulTop li, .mark_answer li, .option-item, .optionItem, .answer li, label, li');
    for (const node of nodes) {
      const content = normalize(node.innerText || node.textContent || '');
      if (!content || content.length > 800) continue;
      // Chaoxing often renders the letter and the choice on separate lines, with no "A." punctuation.
      const m = content.match(/^([A-H])(?:\s*[.．、:：)）]\s*|\s+|(?![\sA-Za-z]))([\s\S]*\S)$/);
      const tf = content.match(/^(正确|错误|对|错)\s*$/);
      const letter = m ? m[1] : tf ? tf[1] : null;
      if (!letter) continue;
      // Prefer short, specific option nodes to wrappers containing nested options.
      if (!opts.has(letter) || opts.get(letter).content.length > content.length) opts.set(letter, { node, content });
    }
    if (opts.size < 2) {
      const lines = (root.innerText || '').split(/\n/).map(normalize).filter(Boolean);
      const candidates = [...root.querySelectorAll('li, label, a, span, i, div')];
      for (let i = 0; i < lines.length - 1; i++) {
        if (!/^[A-H]$/.test(lines[i])) continue;
        const letter = lines[i];
        const text = lines[i + 1];
        if (/^[A-H]$/.test(text) || text.length > 400) continue;
        const wanted = (letter + text).replace(/\s+/g, '');
        let best = null;
        let bestLen = Infinity;
        for (const node of candidates) {
          const content = normalize(node.innerText || node.textContent || '').replace(/\s+/g, '');
          if (!content || content.length > 800) continue;
          if (content !== letter && content !== wanted && !content.startsWith(wanted)) continue;
          if (content.length < bestLen) { best = node; bestLen = content.length; }
        }
        if (!best || opts.has(letter)) { i += 1; continue; }
        const host = best.closest('li, label') || best;
        opts.set(letter, { node: host, content: letter + ' ' + text });
        i += 1;
      }
    }
    if (opts.size === 0 && /单选|多选|判断/.test(first)) {
      // Selector fallback for pages with custom options without A/B prefixes.
      const entries = root.querySelectorAll('input[type="radio"],input[type="checkbox"]');
      [...entries].slice(0, 8).forEach((node, i) => {
        const label = node.closest('label,li') || node.parentElement;
        if (label) opts.set(String.fromCharCode(65 + i), { node: label, content: normalize(label.innerText) });
      });
    }
    if (type === 'unknown' && opts.size >= 2) type = root.querySelectorAll('input[type="checkbox"]').length ? 'multiple' : 'single';
    if (!['single', 'multiple', 'true_false', 'blank'].includes(type)) continue;
    const id = 'shq_' + results.length;
    root.setAttribute('data-study-helper-q', id);
    const options = [];
    for (const [letter, item] of opts.entries()) {
      item.node.setAttribute('data-study-helper-option', letter);
      options.push({ key: letter, text: item.content.slice(0, 800) });
    }
    const visuals = [...root.querySelectorAll('img,svg,canvas')].filter(el => {
      if (el.tagName.toLowerCase() === 'img') return !!(el.getAttribute('src') || el.getAttribute('data-src'));
      return true;
    }).length;
    results.push({ id, type, question, options, has_visual: visuals > 0, full_text: full.slice(0, 3500) });
  }
  return results;
}
