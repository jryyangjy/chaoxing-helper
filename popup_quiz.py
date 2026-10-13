"""Answer a quiz that pops up during real playback. No progress spoofing.

The overlay is read from the page. The visible option is clicked, then the
overlay's own submit control. Captcha and exam hand-in buttons are left alone.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

ROOT = Path(__file__).resolve().parent
PENDING = ROOT / 'output' / 'popup_pending.json'
REPLY = ROOT / 'output' / 'popup_reply.json'

DETECT_JS = r'''() => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 40 && r.height > 24 && st.display !== 'none'
      && st.visibility !== 'hidden' && Number(st.opacity || 1) > 0.05;
  };
  const boxes = [...document.querySelectorAll(
    '.ans-videoquiz, .videoquiz, [class*="videoquiz"], .tkTopic, .ans-insertvideo-quiz'
  )].filter(visible);
  let box = boxes[0] || null;
  if (!box) {
    const nodes = [...document.querySelectorAll('div,section,form')].filter(el => {
      if (!visible(el)) return false;
      const t = el.innerText || '';
      return t.length > 12 && t.length < 1200 && /提交|继续学习/.test(t) && /[A-D]\s*[.．、]/.test(t);
    });
    nodes.sort((a, b) => a.innerText.length - b.innerText.length);
    box = nodes[0] || null;
  }
  if (!box) return null;
  const text = norm(box.innerText).slice(0, 1800);
  if (/验证码|滑动验证|人脸识别|人机验证/.test(text)) return {blocked: 'verification'};
  if (/继续学习/.test(text) && /答对了|回答正确|恭喜/.test(text)) {
    const btn = [...box.querySelectorAll('a,button,span,div')].find(el => norm(el.innerText) === '继续学习' && visible(el));
    if (btn) btn.setAttribute('data-study-helper-continue', '1');
    box.setAttribute('data-study-helper-popup', '1');
    return {continue_learning: true, text};
  }
  const opts = [];
  const seen = new Set();
  for (const node of box.querySelectorAll('li, label, .ans-videoquiz-opt')) {
    const content = norm(node.innerText || '');
    const matched = content.match(/^([A-H])(?:\s*[.．、:：)）]\s*|\s+|(?![\sA-Za-z]))([\s\S]*\S)$/);
    if (!matched || seen.has(matched[1])) continue;
    seen.add(matched[1]);
    node.setAttribute('data-study-helper-pop', matched[1]);
    opts.push({key: matched[1], text: content.slice(0, 400)});
  }
  if (opts.length < 2) {
    const lines = (box.innerText || '').split(/\n/).map(norm).filter(Boolean);
    const candidates = [...box.querySelectorAll('li, label, a, span, i, div')];
    for (let i = 0; i < lines.length - 1; i++) {
      if (!/^[A-H]$/.test(lines[i])) continue;
      const letter = lines[i];
      const text = lines[i + 1];
      if (seen.has(letter) || /^[A-H]$/.test(text)) continue;
      const wanted = (letter + text).replace(/\s+/g, '');
      let best = null;
      let bestLen = Infinity;
      for (const node of candidates) {
        const content = norm(node.innerText || '').replace(/\s+/g, '');
        if (!content || (content !== letter && content !== wanted && !content.startsWith(wanted))) continue;
        if (content.length < bestLen) { best = node; bestLen = content.length; }
      }
      const host = best ? (best.closest('li, label') || best) : null;
      if (!host) continue;
      seen.add(letter);
      host.setAttribute('data-study-helper-pop', letter);
      opts.push({key: letter, text: (letter + ' ' + text).slice(0, 400)});
      i += 1;
    }
  }
  if (opts.length < 2) return null;
  box.setAttribute('data-study-helper-popup', '1');
  const titleNode = box.querySelector('.ans-videoquiz-title, .tkTopic_title, .title');
  const question = norm(titleNode ? titleNode.innerText : text).slice(0, 900);
  return {question, options: opts, text};
}'''


def _frames(page, prefer):
    found = []
    if prefer is not None:
        found.append(prefer)
    try:
        for frame in page.frames:
            if frame not in found:
                found.append(frame)
    except PlaywrightError:
        pass
    return found


def detect_popup(page, prefer=None):
    for frame in _frames(page, prefer):
        try:
            data = frame.evaluate(DETECT_JS)
        except PlaywrightError:
            continue
        if data:
            return frame, data
    return None, None


def _click_choice(frame, key: str) -> bool:
    try:
        option = frame.locator(f'[data-study-helper-pop="{key}"]').first
        if not option.count():
            return False
        option.click(timeout=4000)
        return True
    except PlaywrightError:
        return False


def _click_overlay_submit(frame) -> bool:
    root = frame.locator('[data-study-helper-popup="1"]').first
    for name in ('提交', '确定', '完成'):
        try:
            button = root.get_by_text(name, exact=True)
            if button.count() and button.first.is_visible():
                button.first.click(timeout=4000)
                return True
        except PlaywrightError:
            continue
    try:
        button = root.locator('.ans-videoquiz-submit, a.ans-videoquiz-submit, button').first
        if button.count() and button.is_visible():
            label = button.inner_text(timeout=1000)
            if '交卷' in label:
                return False
            button.click(timeout=4000)
            return True
    except PlaywrightError:
        return False
    return False


def _save_shot(frame, selector: str, path: Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        node = frame.locator(selector).first
        if node.count():
            node.screenshot(path=str(path), timeout=5000)
            return True
    except PlaywrightError:
        pass
    try:
        frame.page.screenshot(path=str(path), timeout=5000)
        return True
    except PlaywrightError:
        return False


def _ensure_options(question: dict) -> None:
    """Fill missing option keys from the page text. Letters stay on their own line."""
    if question.get('options'):
        return
    lines = [line.strip() for line in (question.get('full_text') or '').splitlines() if line.strip()]
    options = []
    index = 0
    while index < len(lines) - 1:
        letter = lines[index]
        nxt = lines[index + 1]
        if len(letter) == 1 and letter in 'ABCDEFGH' and not (len(nxt) == 1 and nxt in 'ABCDEFGH'):
            options.append({'key': letter, 'text': nxt[:400]})
            index += 2
        else:
            index += 1
    if len(options) >= 2:
        question['options'] = options


def _suggestion(question: dict, frame=None, selector: str = '[data-study-helper-popup="1"]') -> dict | None:
    from study_helper import validate_suggestion
    _ensure_options(question)
    if REPLY.exists():
        try:
            REPLY.unlink()
        except OSError:
            pass
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    shot = ROOT / 'output' / 'popup_question.png'
    if frame is not None:
        _save_shot(frame, selector, shot)
    PENDING.write_text(json.dumps({
        'question': question.get('question'),
        'options': question.get('options'),
        'type': question.get('type'),
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print('\n     弹出题目，等待解答：' + (question.get('full_text') or question.get('question') or '')[:500])
    for opt in question.get('options') or []:
        print(f'       {opt.get("key")}. {opt.get("text", "")[:80]}')
    deadline = time.time() + 180
    while time.time() < deadline:
        if REPLY.exists():
            try:
                raw = REPLY.read_text(encoding='utf-8').strip()
                if not raw:
                    time.sleep(0.3)
                    continue
                payload = json.loads(raw)
                suggestion = validate_suggestion(question, payload)
                REPLY.unlink()
                PENDING.unlink(missing_ok=True)
                return suggestion
            except (OSError, ValueError, TypeError) as exc:
                print(f'\n     解答未采用：{exc}')
                try:
                    REPLY.unlink()
                except OSError:
                    pass
                time.sleep(0.4)
                continue
        time.sleep(1)
    return None


def answer_video_popup(helper, prefer_frame=None) -> str | None:
    """Click one reasoned choice on a visible in-video quiz.

    Returns 'answered', 'blocked', 'open', or None when no popup is visible.
    """
    page = prefer_frame.page if prefer_frame is not None else helper.page
    frame, data = detect_popup(page, prefer_frame)
    if not data:
        return None
    if data.get('blocked') == 'verification':
        print('\n     播放中出现验证，停止自动作答，请本人处理。')
        return 'blocked'
    if data.get('continue_learning'):
        try:
            button = frame.locator('[data-study-helper-continue="1"]').first
            if button.count():
                button.click(timeout=4000)
            else:
                frame.get_by_text('继续学习', exact=True).first.click(timeout=4000)
            print('\n     题目已答对，点击继续学习，接着播放。')
            time.sleep(0.6)
            return 'answered'
        except PlaywrightError as exc:
            print(f'\n     继续学习按钮未点到：{str(exc)[:80]}')
            return 'open'
    full = (data.get('text') or '').strip()
    title = (data.get('question') or '').strip()
    question_text = full if len(full) > len(title) + 4 else title
    question = {
        'type': 'true_false' if '判断' in question_text[:12] else 'single',
        'question': question_text,
        'options': data.get('options') or [],
        'full_text': full or title,
    }
    if len(question['options']) < 2:
        return 'open'
    suggestion = _suggestion(question, frame, '[data-study-helper-popup="1"]')
    if not suggestion or not suggestion.get('answer'):
        print('\n     弹题还没有可用答案，先不提交。')
        return 'open'
    key = suggestion['answer'][0]
    print(f'\n     弹题选择 {key}（置信度 {suggestion.get("confidence", 0):.2f}）')
    if not _click_choice(frame, key):
        return 'open'
    time.sleep(0.4)
    if not _click_overlay_submit(frame):
        print('     已选中选项，弹层里没有可点的提交按钮。')
        return 'answered'
    time.sleep(0.8)
    return 'answered'


def answer_loaded_questions(helper) -> bool:
    """Fill visible objective questions and click 暂时保存. Does not click 提交 or 交卷."""
    if not helper.questions:
        return False
    for question in helper.questions:
        if question.answer:
            continue
        try:
            inputs = question.frame.locator(
                f'[data-study-helper-q="{question.data.get("id")}"] input')
            enabled = False
            for index in range(min(inputs.count(), 8)):
                if inputs.nth(index).is_enabled():
                    enabled = True
                    break
            if not enabled:
                print('  这道题不能再改，跳过。')
                continue
        except PlaywrightError:
            print('  未能确认题目是否可填写，跳过。')
            continue
        payload = {
            'type': question.data.get('type') or 'single',
            'question': question.data.get('question') or '',
            'options': question.data.get('options') or [],
            'full_text': question.data.get('full_text') or '',
        }
        _ensure_options(payload)
        if payload.get('options') and not question.data.get('options'):
            question.data['options'] = payload['options']
        suggestion = _suggestion(payload, question.frame, f'[data-study-helper-q="{question.data.get("id")}"]')
        question.answer = suggestion
    for question in helper.questions:
        if not question.answer or question.data.get('type') not in ('single', 'multiple', 'true_false'):
            continue
        try:
            ok, reason = helper.fill_choice(question)
            print(f'  章节题{"已填写" if ok else "未写入"}：{reason}')
        except PlaywrightError as exc:
            print(f'  章节题填写失败：{str(exc)[:120]}')
    answered = [q for q in helper.questions if q.answer and q.data.get('type') in ('single', 'multiple', 'true_false')]
    if not answered:
        return False
    frame = answered[0].frame
    try:
        button = frame.get_by_text('暂时保存', exact=True).first
        if button.count() and button.is_visible():
            button.click(timeout=4000)
            print('  已暂时保存草稿，未点击提交。')
            time.sleep(0.8)
            _confirm_submit(frame)
            return True
        print('  没有找到暂时保存，答案留在页面上，未点击提交。')
    except PlaywrightError as exc:
        print(f'  暂时保存失败：{str(exc)[:120]}')
    return False


def _confirm_submit(frame) -> None:
    """Accept the page's own submit confirmation. Leave 交卷 and verification alone."""
    for name in ('确定', '确认'):
        try:
            button = frame.locator('.layui-layer, .popDiv, .maskDiv, [role="dialog"]').get_by_text(name, exact=True)
            if button.count() and button.first.is_visible():
                label = button.first.inner_text(timeout=1000)
                if '交卷' in label:
                    continue
                button.first.click(timeout=3000)
                print('  已确认提交。')
                return
        except PlaywrightError:
            continue
