"""Regression cases for Chaoxing pages that don't change URL or frame identity."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import shutil
from playwright.sync_api import sync_playwright
from chapter_navigator import choose_loaded_page, page_evidence
from batch_runner import _save_failure_snapshot


def _browser(p):
    return p.chromium.launch(headless=True, **({'executable_path':shutil.which('chromium')}
        if shutil.which('chromium') else {}))


def test_same_iframe_url_and_same_video_count_src_changed():
    with sync_playwright() as p:
        browser = _browser(p)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.set_content('''<div class="chapter_item"><button id="change" onclick="
            setTimeout(() => document.querySelector('video').src='/chapter2.webm', 250)
            ">第二课</button></div><video src="/chapter1.webm" controls></video>''')
        before = page_evidence(page)
        assert before['content']
        tabs = set(ctx.pages)
        page.locator('#change').click()
        nav = choose_loaded_page(ctx, page, tabs, before, timeout=3)
        assert nav.page is page and nav.state == 'loaded_inline'
        browser.close()


def test_same_frame_url_question_text_swaps():
    with sync_playwright() as p:
        browser = _browser(p)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.set_content('''<div class="TiMu">问题一：1+1</div>
          <button onclick="setTimeout(()=>document.querySelector('.TiMu').textContent='问题二：2+2',150)">next</button>''')
        before = page_evidence(page)
        tabs = set(ctx.pages)
        page.locator('button').click()
        nav = choose_loaded_page(ctx,page,tabs,before,timeout=3)
        assert nav.page is page and nav.state == 'loaded_inline'
        browser.close()


def test_preexisting_content_must_not_count_as_new_navigation():
    with sync_playwright() as p:
        browser = _browser(p)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.set_content('<div class="TiMu">原来的题目</div><button>next</button>')
        before = page_evidence(page)
        tabs = set(ctx.pages)
        page.locator('button').click()
        nav = choose_loaded_page(ctx,page,tabs,before,timeout=.4)
        assert nav.page is None and nav.state == 'not_loaded'
        browser.close()


def test_existing_named_popup_reused_then_content_changes():
    with sync_playwright() as p:
        browser = _browser(p)
        ctx = browser.new_context()
        catalog = ctx.new_page()
        catalog.set_content('<button id="open" onclick="window.open(\'about:blank\',\'lessonwindow\')">open</button>')
        catalog.locator('#open').click()
        existing = [pg for pg in ctx.pages if pg is not catalog][0]
        existing.set_content('<div class="TiMu">课后题：未加载</div>')
        before = page_evidence(catalog)
        others = {existing:page_evidence(existing)}
        old = set(ctx.pages)
        # Simulates a reused window changing its DOM without navigation.
        existing.evaluate("() => setTimeout(()=>document.querySelector('.TiMu').textContent='课后题：已加载',220)")
        nav = choose_loaded_page(ctx,catalog,old,before,timeout=2,before_other=others)
        assert nav.page is existing and nav.state == 'loaded_existing_tab'
        browser.close()


def test_unrelated_existing_tab_unchanged_not_selected():
    with sync_playwright() as p:
        browser = _browser(p)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.set_content('<div class="chapter_item">目录</div>')
        other = ctx.new_page()
        other.set_content('<div class="TiMu"> unrelated </div>')
        old=set(ctx.pages)
        before=page_evidence(page)
        others={other:page_evidence(other)}
        nav = choose_loaded_page(ctx,page,old,before,timeout=.4,before_other=others)
        assert nav.page is None
        browser.close()


def test_popup_goes_to_login_form_not_success():
    with sync_playwright() as p:
        browser = _browser(p)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.set_content('''<button onclick="let p=window.open('about:blank');
        p.document.write('<form><input type=password></form>')">go</button>''')
        before=page_evidence(page)
        old=set(ctx.pages)
        page.locator('button').click()
        nav=choose_loaded_page(ctx,page,old,before,timeout=2)
        assert nav.page is None and nav.state=='login_required'
        browser.close()


def test_failure_snapshot_removes_query_and_question_text(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import chapter_navigator, study_helper
    monkeypatch.setattr(study_helper, 'OUTPUT', tmp_path)
    fake_page = SimpleNamespace(url='https://mooc2-ans.chaoxing.com/demo?enc=SECRET_TEST_TOKEN',
                                is_closed=lambda:False)
    monkeypatch.setattr(chapter_navigator, 'page_evidence', lambda p: {
        'content':True, 'login':False, 'body_length':500,
        'frames': [('https://mooc1.chaoxing.com/work?token=SECRET_TEST_TOKEN',
                    {'counts':{'.TiMu':1}, 'bodyLength':500, 'ready':'complete',
                     'text':'SECRET_TEST_QUESTION'})]})
    _save_failure_snapshot(SimpleNamespace(pages=[fake_page]), 'popup_blank', 1)
    data=json.loads((tmp_path/'last_failure.json').read_text(encoding='utf8'))
    serialized=json.dumps(data)
    assert 'SECRET_TEST_QUESTION' not in serialized
    assert 'SECRET_TEST_TOKEN' not in serialized
    assert data['reason']=='popup_blank'


def test_real_html5_video_plays_to_ended_without_seek(monkeypatch):
    """1.5s synthetic VP8 WebM, without external networking or media time edits."""
    import base64
    from batch_runner import play_to_end
    from study_helper import Helper
    fixture = Path(__file__).resolve().parent / 'fixtures' / 'sample.webm'
    uri = 'data:video/webm;base64,' + base64.b64encode(fixture.read_bytes()).decode()
    with sync_playwright() as p:
        browser = _browser(p)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.set_content(f'''<video muted controls width=160 height=90 src="{uri}"></video>
            <button class="play-button" onclick="document.querySelector('video').play()">播放</button>''')
        helper = Helper(ctx)
        history = []
        helper.record_video = lambda *args: history.append(args)
        state = play_to_end(helper, page.main_frame, 0, '测试片段', poll=.3, idle_limit=8, max_seconds=8)
        assert state == 'ended_local'
        assert history and history[0][3] == 'ended_local'
        assert page.locator('video').evaluate('(v) => v.ended')
        browser.close()
