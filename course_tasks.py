"""Per-course task state for the courses listed in courses.json. No credentials and no completion spoofing."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

CONFIG = Path(os.environ.get('CHAOXING_COURSES_FILE') or Path(__file__).resolve().parent / 'courses.json')


def load_targets(path: Path = CONFIG) -> tuple[dict, ...]:
    """Read [{"id": ..., "name": ..., "aliases": [...]}, ...]. Missing or invalid file means no targets."""
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return ()
    targets = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or not item.get('id') or not item.get('name'):
            continue
        targets.append({
            'id': re.sub(r'[^\w-]', '_', str(item['id'])),
            'name': str(item['name']),
            'aliases': tuple(str(a) for a in item.get('aliases') or ()),
        })
    return tuple(targets)


TARGETS: tuple[dict, ...] = load_targets()

# Local handling finished. Catalog icons can still lag; these are not platform grades.
RESOLVED_OUTCOMES = frozenset({
    'catalog_marked_complete_skipped',
    'video_ended_verify_platform',
    'questions_submitted',
    'not_pending_skipped',
})

FIND_JS = r'''(names) => {
  const norm = (s) => (s || '').replace(/\s+/g, '').toLowerCase();
  const keys = names.map(name => ({name, key: norm(name)})).filter(x => x.key);
  const nodes = [...document.querySelectorAll(
    'a, [role="link"], .course-name, .courseName, .course_name, .course-title, .courseItem h3, .studyCard .name')];
  const hits = [];
  const seen = new Set();
  for (const el of nodes) {
    const raw = (el.getAttribute('title') || (el.innerText || '').split('\n')[0] || '').replace(/\s+/g, ' ').trim();
    const n = norm(raw);
    if (!n || n.length > 40) continue;
    const matched = keys.find(x => n === x.key || n.startsWith(x.key));
    if (!matched || seen.has(matched.key)) continue;
    const clickable = el.closest('a, [role="link"]') || el;
    const rect = clickable.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) continue;
    seen.add(matched.key);
    clickable.setAttribute('data-study-helper-course', String(hits.length));
    hits.push({index: hits.length, label: raw.slice(0, 80), course_name: matched.name});
  }
  return hits;
}'''

HINT_JS = r'''() => {
  const sel = '.course-name, .courseName, .course_name, #courseName, .courseTitle, .nav_title, .className, h1, h2, .chapter_title, .breadcrumb';
  return [...document.querySelectorAll(sel)]
    .map(el => (el.innerText || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim().slice(0, 80))
    .filter(Boolean)
    .slice(0, 20);
}'''


def normalize(text: str) -> str:
    return re.sub(r'\s+', '', text or '').casefold()


def course_keys(course: dict) -> tuple[str, ...]:
    return (course['name'], *course.get('aliases', ()))


def search_names() -> list[str]:
    names = []
    for course in TARGETS:
        names.extend(course_keys(course))
    return names


def match_course(text: str) -> dict | None:
    """Return the single target named in text. Ambiguous text matches nothing."""
    folded = normalize(text)
    if not folded:
        return None
    hits = []
    for course in TARGETS:
        if any(normalize(key) and normalize(key) in folded for key in course_keys(course)):
            hits.append(course)
    if len(hits) == 1:
        return hits[0]
    return None


def course_dir(output: Path, course_id: str) -> Path:
    return output / 'courses' / course_id


def batch_report_path(output: Path, course_id: str) -> Path:
    return course_dir(output, course_id) / 'batch_report.json'


def load_state(output: Path, course_id: str) -> dict[str, Any]:
    path = course_dir(output, course_id) / 'state.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(output: Path, state: dict[str, Any]) -> None:
    path = course_dir(output, state['course_id']) / 'state.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def summarize(rows: list[dict]) -> dict[str, int]:
    complete = sum(1 for row in rows if row.get('catalog_status') == 'complete')
    pending = sum(1 for row in rows if row.get('catalog_status') == 'pending')
    unknown = sum(1 for row in rows if row.get('catalog_status') == 'unknown')
    attention = sum(
        1 for row in rows
        if row.get('catalog_status') != 'complete'
        and row.get('last_outcome')
        and row.get('last_outcome') not in RESOLVED_OUTCOMES
    )
    total = len(rows)
    return {
        'total': total,
        'complete': complete,
        'pending': pending,
        'unknown': unknown,
        'unfinished': total - complete,
        'needs_attention': attention,
    }


def catalog_rows(chapters, previous: dict | None) -> list[dict]:
    prev_rows = (previous or {}).get('chapters') or []
    by_sig = {row.get('signature'): row for row in prev_rows if row.get('signature')}
    by_label = {row.get('label'): row for row in prev_rows if row.get('label')}
    rows = []
    for index, chapter in enumerate(chapters, 1):
        old = by_sig.get(chapter.signature) or by_label.get(chapter.label) or {}
        rows.append({
            'number': index,
            'label': chapter.label,
            'signature': chapter.signature,
            'catalog_status': chapter.status,
            'pending': chapter.pending,
            'last_outcome': old.get('last_outcome'),
            'last_block_reason': old.get('last_block_reason'),
            'latest_video_progress': old.get('latest_video_progress'),
        })
    return rows


def remember_catalog(output: Path, course: dict, chapters, page_url: str, when: str) -> dict:
    from study_helper import safe_url
    rows = catalog_rows(chapters, load_state(output, course['id']))
    state = {
        'course_id': course['id'],
        'course_name': course['name'],
        'updated': when,
        'page': safe_url(page_url or ''),
        'chapters': rows,
        'summary': summarize(rows),
        'notes': '目录完成标记只反映当前页面；本地播放结束不代表平台已完成。',
    }
    save_state(output, state)
    return state


def update_chapter_progress(output: Path, course_id: str, item: dict, when: str) -> bool:
    """Merge one chapter result. Returns True when the recorded outcome changes."""
    state = load_state(output, course_id)
    if not state.get('chapters'):
        return False
    changed = False
    for row in state['chapters']:
        if row.get('number') != item.get('number'):
            continue
        if item.get('latest_video_progress'):
            row['latest_video_progress'] = item['latest_video_progress']
        outcome = item.get('outcome')
        if outcome and outcome != 'pending' and outcome != row.get('last_outcome'):
            row['last_outcome'] = outcome
            row['last_block_reason'] = item.get('block_reason')
            row['updated'] = when
            changed = True
        break
    state['summary'] = summarize(state['chapters'])
    state['updated'] = when
    save_state(output, state)
    return changed


def prioritize(snapshots: list[dict]) -> list[dict]:
    """Unfinished courses first. Snapshot keys: course, state."""
    return sorted(snapshots, key=lambda snap: (
        0 if snap['state']['summary']['unfinished'] else 1,
        -snap['state']['summary']['unfinished'],
        -snap['state']['summary']['needs_attention'],
        snap['course']['name'],
    ))


def redact(message: str) -> str:
    from study_helper import safe_url
    text = re.sub(
        r'(?i)(token|cookie|authorization|password|passwd|pwd)\s*[=:]\s*\S+',
        r'\1=[已省略]',
        message or '',
    )
    text = re.sub(r'https?://\S+', lambda match: safe_url(match.group(0)), text)
    return text[:500]


def note(output: Path, course_id: str, message: str, when: str) -> None:
    path = course_dir(output, course_id) / 'events.log'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(f'{when} {redact(message)}\n')


def format_summary(state: dict) -> str:
    summary = state.get('summary') or {}
    return (
        f'{state.get("course_name", "课程")}：共 {summary.get("total", 0)} 节，'
        f'页面已完成 {summary.get("complete", 0)}，未完成 {summary.get("unfinished", 0)}'
        f'（待完成 {summary.get("pending", 0)}，未知 {summary.get("unknown", 0)}，'
        f'需人工处理 {summary.get("needs_attention", 0)}）'
    )


def saved_states(output: Path) -> list[dict]:
    found = []
    for course in TARGETS:
        state = load_state(output, course['id'])
        if state.get('chapters'):
            found.append(state)
    return found
