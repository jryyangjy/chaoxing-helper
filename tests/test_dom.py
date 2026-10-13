import sys
import shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright
from study_helper import Helper, EXTRACTOR, validate_suggestion, LocatedQuestion


def test_dom_adapter():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **({'executable_path': shutil.which('chromium')} if shutil.which('chromium') else {}))
        page = browser.new_page()
        page.set_content('''
        <div class="mark_item">
          <div class="TiMu newTiMu">
            <div class="Zy_TItle">1. 下列哪个是质数？（单选题）</div>
            <ul class="answerList"><li><label><input type="radio" name="q1">A. 4</label></li>
            <li><label><input type="radio" name="q1">B. 5</label></li></ul>
          </div>
          <div class="TiMu newTiMu">
            <div class="Zy_TItle">2. 以下是偶数的有？（多选题）</div>
            <ul class="answerList"><li><label><input type="checkbox">A. 6</label></li>
            <li><label><input type="checkbox">B. 7</label></li>
            <li><label><input type="checkbox">C. 8</label></li></ul>
          </div>
        </div>''')
        rows = page.main_frame.evaluate(EXTRACTOR)
        print(rows)
        assert len(rows) == 2
        assert rows[0]['type'] == 'single'
        assert [x['key'] for x in rows[0]['options']] == ['A', 'B']
        assert rows[1]['type'] == 'multiple'
        ctx = page.context
        helper = Helper(ctx)
        q = LocatedQuestion(page, page.main_frame, rows[0], 0, 0,
                            validate_suggestion(rows[0], {'answer': ['B']}))
        ok, reason = helper.fill_choice(q)
        assert ok, reason
        assert page.locator('input[type="radio"]').nth(1).is_checked()
        browser.close()


def test_chaoxing_options_without_punctuation():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **({'executable_path': shutil.which('chromium')} if shutil.which('chromium') else {}))
        page = browser.new_page()
        page.set_content('''
        <div class="TiMu">
          <div class="Zy_TItle">1【单选题】哪次会议确立政策？（20.0分）</div>
          <ul class="Zy_ulTop">
            <li><i>A</i><a>八七会议</a></li>
            <li><i>B</i><a>遵义会议</a></li>
            <li><i>C</i><a>洛川会议</a></li>
            <li><i>D</i><a>瓦窑堡会议</a></li>
          </ul>
        </div>''')
        rows = page.main_frame.evaluate(EXTRACTOR)
        assert len(rows) == 1
        assert [x['key'] for x in rows[0]['options']] == ['A', 'B', 'C', 'D']
        ctx = page.context
        helper = Helper(ctx)
        q = LocatedQuestion(page, page.main_frame, rows[0], 0, 0,
                            validate_suggestion(rows[0], {'answer': ['D'], 'confidence': 0.9}))
        ok, reason = helper.fill_choice(q)
        assert ok, reason
        browser.close()


def test_chaoxing_options_without_punctuation():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **({'executable_path': shutil.which('chromium')} if shutil.which('chromium') else {}))
        page = browser.new_page()
        page.set_content('''
        <div class="TiMu">
          <div class="Zy_TItle">1【单选题】哪次会议确立政策？（20.0分）</div>
          <ul class="Zy_ulTop">
            <li><i>A</i><a>八七会议</a></li>
            <li><i>B</i><a>遵义会议</a></li>
            <li><i>C</i><a>洛川会议</a></li>
            <li><i>D</i><a>瓦窑堡会议</a></li>
          </ul>
        </div>''')
        rows = page.main_frame.evaluate(EXTRACTOR)
        assert len(rows) == 1
        assert [x['key'] for x in rows[0]['options']] == ['A', 'B', 'C', 'D']
        ctx = page.context
        helper = Helper(ctx)
        q = LocatedQuestion(page, page.main_frame, rows[0], 0, 0,
                            validate_suggestion(rows[0], {'answer': ['D'], 'confidence': 0.9}))
        ok, reason = helper.fill_choice(q)
        assert ok, reason
        browser.close()


def test_nested_iframe_scan():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **({'executable_path': shutil.which('chromium')} if shutil.which('chromium') else {}))
        page = browser.new_page()
        page.set_content('''<iframe id="iframe" srcdoc="<iframe srcdoc=''' + "'&lt;div class=&quot;TiMu&quot;&gt;&lt;div class=&quot;Zy_TItle&quot;&gt;（判断题）地球是球体&lt;/div&gt;&lt;ul class=&quot;answerList&quot;&gt;&lt;li&gt;正确&lt;/li&gt;&lt;li&gt;错误&lt;/li&gt;&lt;/ul&gt;&lt;/div&gt;'" + '''></iframe>"></iframe>''')
        page.wait_for_timeout(100)
        found = []
        for f in page.frames:
            found.extend(f.evaluate(EXTRACTOR))
        assert any(q['type'] == 'true_false' for q in found)
        browser.close()
