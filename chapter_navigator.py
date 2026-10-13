"""Defensive chapter navigation across new, existing, and inline tabs/frames.

No completion reports without visible DOM or local playback evidence.
"""
from __future__ import annotations
import time
from dataclasses import dataclass
from pathlib import Path
from playwright.sync_api import Error as PlaywrightError

READY_JS = (Path(__file__).resolve().parent / 'page_ready.js').read_text(encoding='utf-8')


@dataclass
class NavigationResult:
    page: object | None
    state: str
    details: str


def page_evidence(page) -> dict:
    """Read-only signals; signatures are computed on-page and never retain task text."""
    evidence = {'url': page.url, 'content': False, 'frames': [],
                'body_length': 0, 'login': False}
    if page.is_closed():
        return evidence
    for frame in list(page.frames):
        try:
            got = frame.evaluate(READY_JS) or {}
            if not isinstance(got, dict):
                continue
            evidence['frames'].append((frame.url, got))
            evidence['content'] |= got['content']
            evidence['login'] |= got['login']
            evidence['body_length'] = max(evidence['body_length'], got['bodyLength'])
        except PlaywrightError:
            continue
    return evidence


def _task_changed(before: dict | None, after: dict) -> bool:
    """Recognize in-place chapter swaps that keep the same page and frame URL."""
    if not after['content']:
        return False
    if not before or not before['content']:
        return True
    if after['url'] != before['url']:
        return True
    def sigs(e):
        return sorted((url, doc.get('contentSig', ''), doc.get('content', False))
                      for url, doc in e['frames'])
    return sigs(after) != sigs(before)


def choose_loaded_page(context, catalog, before_pages, before_catalog,
                       timeout=25, poll=.35, before_other=None):
    """Wait for actual chapter content rather than fixed time or title change.

    A reused named browser tab and an existing iframe whose DOM changes in place
    are valid destinations. An unchanged task/sidebar is *not* a success.
    """
    before_other = before_other or {}
    deadline = time.monotonic() + timeout
    blank_seen = False
    nonempty_since = {}
    while time.monotonic() < deadline:
        live = [p for p in context.pages if not p.is_closed()]
        # Newly created popup and reused named tabs may both receive content.
        for page in live:
            if page is catalog:
                continue
            new = page not in before_pages
            baseline = None if new else before_other.get(page)
            if page.url == 'about:blank':
                if new:
                    blank_seen = True
                # about:blank can also host dynamically written HTML.
            after = page_evidence(page)
            if after['login'] and (new or _task_changed(baseline,after)):
                return NavigationResult(None, 'login_required', '跳转到了登录页，请本人重新登录')
            if _task_changed(baseline, after):
                return NavigationResult(page, 'loaded_popup' if new else 'loaded_existing_tab',
                                        '新窗口加载了任务' if new else '已有标签页已切换到任务内容')
            # Unknown destinations only count after stable page navigation,
            # and must be checked manually; not treated as completed lessons.
            if page.url != 'about:blank' and after['body_length'] > 80 and (
                    new or (baseline and page.url != baseline['url'])):
                key = id(page)
                nonempty_since.setdefault(key, time.monotonic())
                if time.monotonic() - nonempty_since[key] > 6:
                    return NavigationResult(page, 'popup_nonempty_unverified',
                                            '已加载页面，但无法识别任务结构')

        if catalog in live:
            after = page_evidence(catalog)
            if after['login'] and not before_catalog['login']:
                return NavigationResult(None, 'login_required', '课程页跳回登录界面')
            if _task_changed(before_catalog, after):
                return NavigationResult(catalog, 'loaded_inline',
                                        '当前标签或已有 iframe 内容发生变化')
            if catalog.url != before_catalog['url'] and catalog.url != 'about:blank' and after['body_length'] > 80:
                nonempty_since.setdefault(id(catalog), time.monotonic())
                if time.monotonic() - nonempty_since[id(catalog)] > 6:
                    return NavigationResult(catalog, 'inline_nonempty_unverified',
                                            '当前页面已跳转，但未找到支持的任务')
        try:
            if catalog in live:
                catalog.wait_for_timeout(max(80, int(poll*1000)))
            else:
                time.sleep(poll)
        except PlaywrightError:
            time.sleep(poll)
    if blank_seen:
        return NavigationResult(None, 'popup_blank',
                                '弹出的窗口未载入任务内容（可能仍是 about:blank）')
    return NavigationResult(None, 'not_loaded', '没有发现新任务内容；可能需要展开章节或已处于该章节')
