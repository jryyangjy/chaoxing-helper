from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import shutil
from playwright.sync_api import sync_playwright
from batch_runner import discover_chapters, click_chapter
from chapter_navigator import choose_loaded_page, page_evidence


def launch(p):
    return p.chromium.launch(headless=True, **({'executable_path':shutil.which('chromium')}
        if shutil.which('chromium') else {}))


def test_mooc2_chapter_completion_and_pending():
    with sync_playwright() as p:
        browser = launch(p)
        page=browser.new_page()
        page.set_content('''<div class="chapter_unit">
          <div class="chapter_item" id="cur23"><div class="catalog_sbar">2.3</div>
            <a class="clicktitle">课后习题</a><div class="catalog_task"><i class="icon_yiwanc"></i></div></div>
          <div class="chapter_item" id="cur24"><div class="catalog_sbar">2.4</div>
            <a class="clicktitle">推荐阅读书目</a><div class="catalog_task"><span class="catalog_points_yi">1</span></div></div>
          <div class="chapter_item" id="cur25"><a class="clicktitle">2.5 视频</a></div>
        </div>''')
        chapters=discover_chapters(page)
        assert [c.label for c in chapters] == ['2.3 课后习题','2.4 推荐阅读书目','2.5 视频']
        assert [c.status for c in chapters] == ['complete','pending','unknown']
        browser.close()


def test_loaded_inline_when_video_tag_arrives_late():
    with sync_playwright() as p:
        browser=launch(p)
        ctx=browser.new_context()
        page=ctx.new_page()
        page.set_content('''<div class="chapter_item" id="cur12"><a class="clicktitle"
        onclick="setTimeout(()=>document.querySelector('#lesson').innerHTML='<video controls></video>',400)">1.2 视频学习</a></div><div id="lesson"></div>''')
        chapters=discover_chapters(page)
        before=page_evidence(page); tabs=set(ctx.pages)
        assert click_chapter(page,chapters[0])
        nav=choose_loaded_page(ctx,page,tabs,before,timeout=3)
        assert nav.page is page and nav.state == 'loaded_inline'
        browser.close()


def test_empty_popup_but_real_inline_content_wins():
    with sync_playwright() as p:
        browser=launch(p)
        ctx=browser.new_context()
        page=ctx.new_page()
        page.set_content('''<button onclick="window.open('about:blank');setTimeout(()=>
             document.querySelector('#content').innerHTML='<div class=TiMu>测试题 1+1=?</div>',200)">open</button>
             <div id="content"></div>''')
        before=page_evidence(page); tabs=set(ctx.pages)
        page.locator('button').click()
        nav=choose_loaded_page(ctx,page,tabs,before,timeout=3)
        assert nav.state == 'loaded_inline'
        browser.close()


def test_blank_popup_does_not_count_as_chapter_done():
    with sync_playwright() as p:
        browser=launch(p)
        ctx=browser.new_context()
        page=ctx.new_page()
        page.set_content('<button onclick="window.open(\'about:blank\')">open</button>')
        before=page_evidence(page);tabs=set(ctx.pages)
        page.locator('button').click()
        nav=choose_loaded_page(ctx,page,tabs,before,timeout=1)
        assert nav.page is None and nav.state == 'popup_blank'
        browser.close()


def test_unchanged_catalog_not_interpreted_as_loaded():
    with sync_playwright() as p:
        browser=launch(p)
        ctx=browser.new_context()
        page=ctx.new_page()
        page.set_content('<div class="chapter_item"><a class="clicktitle">2.1 视频</a></div>')
        before=page_evidence(page);tabs=set(ctx.pages)
        nav=choose_loaded_page(ctx,page,tabs,before,timeout=.4)
        assert nav.page is None and nav.state == 'not_loaded'
        browser.close()
