import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright
from study_helper import Helper
from navigation_debug import capture_navigation, safe_message


def test_navigation_debug_keeps_url_queries_out_of_messages():
    raw = 'Failed loading https://example.test/course?enc=mysessionsecret&courseid=12345, token:superprivate'
    clean = safe_message(raw)
    assert 'mysessionsecret' not in clean
    assert 'superprivate' not in clean
    assert 'https://example.test/course' in clean


def test_navigation_debug_detects_new_blank_page():
    with sync_playwright() as p:
        import shutil
        browser = p.chromium.launch(headless=True, **({'executable_path': shutil.which('chromium')}
                                                       if shutil.which('chromium') else {}))
        context = browser.new_context()
        page = context.new_page()
        page.set_content('''<button id="go" onclick="window.open('about:blank')">打开课程</button>''')
        # Synthetic click; test checks the inspector, not the Chaoxing site.
        page.evaluate("setTimeout(()=>document.querySelector('#go').click(), 120)")
        log = capture_navigation(Helper(context), seconds=1.3, tick=0.15)
        assert any(x['kind']=='tab_opened' for x in log['events'])
        assert any(x['url']=='about:blank' for x in log['final_tabs'])
        context.close()
        browser.close()
