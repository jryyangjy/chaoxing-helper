import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from playwright.sync_api import sync_playwright

from batch_runner import discover_chapters, click_chapter, resolve_course_page
from local_ai import ask_ollama
from study_helper import validate_suggestion


def test_chapters_scan_and_click():
    with sync_playwright() as p:
        import shutil
        browser = p.chromium.launch(headless=True, **({'executable_path': shutil.which('chromium')}
                                                       if shutil.which('chromium') else {}))
        page = browser.new_page()
        page.set_content('''
          <div class="posCatalog_name" onclick="document.querySelector('#selected').textContent='one'">第1章 绪论</div>
          <div class="posCatalog_name" onclick="document.querySelector('#selected').textContent='two'">第2章 基础</div>
          <div id="selected"></div>''')
        items = discover_chapters(page)
        assert len(items) == 2, items
        assert click_chapter(page, items[1])
        assert page.locator('#selected').text_content() == 'two'
        browser.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass
    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        data = json.loads(self.rfile.read(length))
        assert self.path == '/api/chat'
        assert data['stream'] is False and data['format'] == 'json'
        assert 'question' in data['messages'][1]['content']
        content = json.dumps({'answer': ['B'], 'explanation': '根据题干', 'confidence': 0.7})
        reply = json.dumps({'message': {'content': content}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


def test_local_ollama_protocol():
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        question = {'type': 'single', 'question': '2+2=?', 'options': [
            {'key': 'A', 'text': '3'}, {'key': 'B', 'text': '4'}], 'full_text': '2+2=?'}
        result = ask_ollama(question, endpoint=f'http://127.0.0.1:{server.server_port}')
        assert validate_suggestion(question, result)['answer'] == ['B']
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_ollama_nonlocal_rejected():
    with pytest.raises(ValueError, match='只允许本机'):
        ask_ollama({}, endpoint='http://evil.invalid:11434')


def test_mooc2_chapter_item_clicktitle_and_tab_switch():
    from study_helper import Helper
    with sync_playwright() as p:
        import shutil
        browser = p.chromium.launch(headless=True, **({'executable_path': shutil.which('chromium')}
                                                       if shutil.which('chromium') else {}))
        ctx = browser.new_context()
        initial_tab = ctx.new_page()
        initial_tab.set_content('<h1>个人空间</h1>')
        helper = Helper(ctx)  # created before the user opens a new tab
        lesson_tab = ctx.new_page()
        lesson_tab.set_content('''
           <div class="chapter_unit">
             <div class="catalog_name"><a class="clicktitle">第一章</a></div>
             <div class="chapter_item" id="cur11"><a class="clicktitle" onclick="document.querySelector('#picked').textContent='11'">1.1 知识点简介</a></div>
             <div class="chapter_item" id="cur12"><a class="clicktitle" onclick="document.querySelector('#picked').textContent='12'">1.2 视频学习</a></div>
             <div class="chapter_item" id="cur13"><a class="clicktitle" onclick="document.querySelector('#picked').textContent='13'">1.3 课后习题</a></div>
           </div><div id="picked"></div>
        ''')
        page, chapters = resolve_course_page(helper)
        assert page == lesson_tab
        assert helper.page == lesson_tab
        assert [x.label for x in chapters] == ['1.1 知识点简介', '1.2 视频学习', '1.3 课后习题']
        assert click_chapter(page, chapters[1])
        assert page.locator('#picked').text_content() == '12'
        browser.close()

def test_same_title_different_chapter_item_ids():
    with sync_playwright() as p:
        import shutil
        browser = p.chromium.launch(headless=True, **({'executable_path': shutil.which('chromium')}
                                                       if shutil.which('chromium') else {}))
        page = browser.new_page()
        page.set_content('''<div class="chapter_unit">
            <div class="chapter_item" id="cur11"><a class="clicktitle">视频学习</a></div>
            <div class="chapter_item" id="cur21"><a class="clicktitle">视频学习</a></div>
        </div>''')
        rows=discover_chapters(page)
        assert len(rows)==2
        browser.close()
