"""Diagnostic-only observer for navigation failures in the user's own browser session.

Logs only sanitized URLs and limited error messages. Never modifies cookies, routes,
user agent, media timing, or navigation targets.
"""
from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING
from playwright.sync_api import Error as PlaywrightError

if TYPE_CHECKING:
    from study_helper import Helper


def safe_message(message: str) -> str:
    """Best-effort conceal URLs with query strings and long token-like substrings."""
    from study_helper import safe_url
    message = str(message)
    message = re.sub(r'https?://[^\s\"\'<>]+', lambda m: safe_url(m.group(0)), message)
    message = re.sub(r'(?i)(token|cookie|authorization|session|enc|cpi|clazzid|courseid)\s*[=:]\s*[^,\s;}]+',
                     r'\1=[REDACTED]', message)
    return re.sub(r'[A-Za-z0-9_\-]{50,}', '[LONG_VALUE_REDACTED]', message)[:220]


def tab_state(page):
    from study_helper import safe_url
    try:
        state = page.evaluate('''() => ({ready:document.readyState,
            bodyLength:(document.body?.innerText || '').length,
            iframeCount:document.querySelectorAll('iframe,frameset,frame').length})''')
    except PlaywrightError:
        state = {'ready':'unreadable', 'bodyLength':None,'iframeCount':None}
    try:
        title = page.title()[:90]
    except PlaywrightError:
        title = '[unavailable]'
    return {'url':safe_url(page.url), 'title':title, 'closed':page.is_closed(),
            'frame_count':len(page.frames), **state}


def capture_navigation(helper: 'Helper', seconds: float = 35, tick: float = 0.5):
    """Ask user to click a course during a short read-only Playwright event capture."""
    from study_helper import OUTPUT, save_json, safe_url, stamp
    ctx = helper.ctx
    log = {'time': stamp(), 'duration_seconds': seconds, 'events': [], 'initial_tabs': [], 'final_tabs': []}
    active = {}
    handlers = []
    def record(kind, **fields):
        if len(log['events']) >= 250:
            return
        log['events'].append({'second':round(time.monotonic()-start,1), 'kind':kind, **fields})

    def listen(target, kind, callback):
        target.on(kind, callback)
        handlers.append((target, kind, callback))

    def attach(page):
        if id(page) in active:
            return
        active[id(page)] = page
        record('tab_opened', url=safe_url(page.url))
        listen(page, 'framenavigated', lambda frame, pg=page: record('frame_navigation',
            tab=safe_url(pg.url), frame=safe_url(frame.url), main=(frame==pg.main_frame)))
        listen(page, 'pageerror', lambda err, pg=page: record('page_js_error',
            tab=safe_url(pg.url), message=safe_message(str(err))))
        listen(page, 'console', lambda msg, pg=page: record('console_'+msg.type,
            tab=safe_url(pg.url), message=safe_message(msg.text)) if msg.type=='error' else None)

    start = time.monotonic()
    for pg in ctx.pages:
        if not pg.is_closed():
            log['initial_tabs'].append(tab_state(pg))
            attach(pg)
    listen(ctx, 'page', attach)
    listen(ctx, 'requestfailed', lambda req: record('request_failed',
        url=safe_url(req.url), resource_type=req.resource_type,
        reason=safe_message(str(req.failure or 'unknown'))))
    listen(ctx, 'response', lambda resp: record('http_error', url=safe_url(resp.url),
        status=resp.status, resource_type=resp.request.resource_type)
        if resp.status >= 400 and resp.request.resource_type in ('document','script') else None)
    print(f'诊断监控已启动（{seconds:.0f} 秒）。现在请在浏览器打开“个人空间”并点击一次课程。')
    print('不要输入账号密码到终端；只需正常操作浏览器。')
    try:
        while time.monotonic()-start < seconds:
            pages = [p for p in ctx.pages if not p.is_closed()]
            if not pages:
                break
            try:
                pages[0].wait_for_timeout(max(100,int(tick*1000)))
            except PlaywrightError:
                time.sleep(min(0.1,tick))
    except KeyboardInterrupt:
        print('\n诊断提前结束，保留已收到的信息。')
    finally:
        for target, kind, callback in handlers:
            try:
                target.remove_listener(kind, callback)
            except Exception:
                pass
        for pg in ctx.pages:
            if not pg.is_closed():
                log['final_tabs'].append(tab_state(pg))
        path = OUTPUT / 'navigation_diagnostic.json'
        save_json(path, log)
    blank_tabs = [x for x in log['final_tabs'] if x['url']=='about:blank']
    print(f'\n诊断已保存：{path}；新开标签/跳转/异常事件共 {len(log["events"])} 条。')
    if blank_tabs:
        print(f'目前检测到 {len(blank_tabs)} 个 about:blank 标签页。')
        if all((x.get('bodyLength') or 0)==0 for x in blank_tabs):
            print('这些空白页未显示正文；可能是网站没有赋予跳转地址，或新标签页加载中断。')
        else:
            print('注意：about:blank 也可能承载由页面动态写入的内容；请查看诊断中 bodyLength/iframeCount。')
    print('请先检查文件中是否包含个人信息，再将 navigation_diagnostic.json 发给我。')
    return log
