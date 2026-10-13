"""Per-course state for configured courses. No playback spoofing."""
import json
import shutil
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from batch_runner import Chapter, _resume_suggestion, survey_target_courses
from course_tasks import (
    format_summary, load_state, match_course, note, prioritize, remember_catalog, summarize,
)
from study_helper import Helper

COURSES = (
    {'id': 'history', 'name': '近代史', 'aliases': ('中国近现代史纲要',)},
    {'id': 'ai_intro', 'name': '人工智能导论', 'aliases': ()},
)


@pytest.fixture(autouse=True)
def configured_courses(monkeypatch):
    import course_tasks
    monkeypatch.setattr(course_tasks, 'TARGETS', COURSES)


def launch(p):
    return p.chromium.launch(
        headless=True,
        **({'executable_path': shutil.which('chromium')} if shutil.which('chromium') else {}),
    )


def test_match_only_configured_courses():
    assert match_course('近代史')['id'] == 'history'
    assert match_course('中国近现代史纲要（2026）')['id'] == 'history'
    assert match_course('人工智能导论')['id'] == 'ai_intro'
    assert match_course('人工智能导论-章节')['id'] == 'ai_intro'
    assert match_course('高等数学') is None
    assert match_course('近代史 与 人工智能导论') is None
    assert match_course('') is None


def test_unfinished_course_is_ordered_first():
    ranked = prioritize([
        {'course': {'id': 'history', 'name': '近代史'},
         'state': {'summary': {'unfinished': 0, 'needs_attention': 0}}},
        {'course': {'id': 'ai_intro', 'name': '人工智能导论'},
         'state': {'summary': {'unfinished': 4, 'needs_attention': 1}}},
    ])
    assert [item['course']['id'] for item in ranked] == ['ai_intro', 'history']


def test_resume_follows_course_id_across_url_changes(tmp_path):
    report = tmp_path / 'batch_report.json'
    chapters = [Chapter('1', '1.1 视频', 'about:blank'), Chapter('2', '1.2 视频', 'about:blank')]
    report.write_text(json.dumps({
        'course_id': 'history',
        'initial_course_page': 'https://example.org/old',
        'chapters': [
            {'number': 1, 'chapter': '1.1 视频', 'outcome': 'catalog_marked_complete_skipped'},
            {'number': 2, 'chapter': '1.2 视频', 'outcome': 'video_needs_manual_attention'},
        ],
    }), encoding='utf-8')
    assert _resume_suggestion(report, chapters, 'https://elsewhere.org/new?token=SECRET', 'history') == 2
    assert _resume_suggestion(report, chapters, 'https://elsewhere.org/new', 'ai_intro') == 1


def test_rescan_keeps_prior_outcome_and_log_drops_secrets(tmp_path):
    course = {'id': 'history', 'name': '近代史'}
    first = Chapter('sig-1', '1.1 绪论', 'about:blank')
    first.status = 'pending'
    first.pending = '1'
    state = remember_catalog(tmp_path, course, [first], 'https://example.org/course?token=SECRET', 't0')
    state['chapters'][0]['last_outcome'] = 'video_needs_manual_attention'
    (tmp_path / 'courses' / 'history' / 'state.json').write_text(
        json.dumps(state, ensure_ascii=False), encoding='utf-8')
    again = remember_catalog(tmp_path, course, [first], 'https://example.org/course', 't1')
    assert again['chapters'][0]['last_outcome'] == 'video_needs_manual_attention'
    assert 'token=' not in again['page']
    note(tmp_path, 'history', 'password=secret https://example.org/x?token=abc', 't2')
    log = (tmp_path / 'courses' / 'history' / 'events.log').read_text(encoding='utf-8')
    assert 'secret' not in log
    assert 'token=abc' not in log
    assert '未完成 1' in format_summary(again)


def test_summary_counts_catalog_status():
    rows = [
        {'catalog_status': 'complete', 'last_outcome': 'catalog_marked_complete_skipped'},
        {'catalog_status': 'pending', 'last_outcome': 'video_needs_manual_attention'},
        {'catalog_status': 'unknown', 'last_outcome': None},
    ]
    assert summarize(rows) == {
        'total': 3, 'complete': 1, 'pending': 1, 'unknown': 1, 'unfinished': 2, 'needs_attention': 1,
    }


def test_survey_stores_courses_separately_and_ranks_unfinished(tmp_path, monkeypatch):
    import study_helper
    monkeypatch.setattr(study_helper, 'OUTPUT', tmp_path)
    monkeypatch.setattr('builtins.input', lambda _='': pytest.fail('scan must not prompt'))
    with sync_playwright() as p:
        browser = launch(p)
        ctx = browser.new_context()
        done = ctx.new_page()
        done.set_content('''<title>近代史</title>
          <div class="chapter_item" id="a1"><span class="catalog_sbar">1</span>
            <a class="clicktitle">已学完</a>
            <div class="catalog_task"><i class="icon_yiwanc"></i></div></div>''')
        todo = ctx.new_page()
        todo.set_content('''<title>人工智能导论</title>
          <div class="chapter_item" id="b1"><span class="catalog_sbar">1</span>
            <a class="clicktitle">第1节</a>
            <div class="catalog_task"><span class="catalog_points_yi">1</span></div></div>
          <div class="chapter_item" id="b2"><span class="catalog_sbar">2</span>
            <a class="clicktitle">第2节</a>
            <div class="catalog_task"><span class="catalog_points_yi">1</span></div></div>''')
        helper = Helper(ctx)
        ranked = survey_target_courses(helper, interactive=False)
        browser.close()
    assert [item['course']['id'] for item in ranked] == ['ai_intro', 'history']
    ai = load_state(tmp_path, 'ai_intro')
    hist = load_state(tmp_path, 'history')
    assert ai['summary']['unfinished'] == 2
    assert hist['summary']['complete'] == 1
    assert ai['course_name'] == '人工智能导论'
    assert not (tmp_path / 'batch_report.json').exists()


def test_survey_opens_course_card_without_playing(tmp_path, monkeypatch):
    import study_helper
    monkeypatch.setattr(study_helper, 'OUTPUT', tmp_path)
    with sync_playwright() as p:
        browser = launch(p)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.set_content('''<title>我的课程</title>
          <a class="course-name" onclick="document.getElementById('box').style.display='block'">近代史</a>
          <div id="box" style="display:none">
            <div class="chapter_item" id="c1"><span class="catalog_sbar">1.1</span>
              <a class="clicktitle">绪论</a>
              <div class="catalog_task"><span class="catalog_points_yi">2</span></div>
            </div>
          </div>''')
        helper = Helper(ctx)
        ranked = survey_target_courses(helper, interactive=False)
        assert page.locator('video').count() == 0
        browser.close()
    assert len(ranked) == 1
    assert ranked[0]['course']['id'] == 'history'
    assert load_state(tmp_path, 'history')['chapters'][0]['label'].endswith('绪论')
    assert load_state(tmp_path, 'ai_intro') == {}


def test_named_course_batch_keeps_blocked_video_and_checkpoint(tmp_path, monkeypatch):
    import batch_runner
    import study_helper
    monkeypatch.setattr(study_helper, 'OUTPUT', tmp_path)
    answers = iter(['1', '1', 'Y', '0', 'Y', 'RUN'])
    monkeypatch.setattr('builtins.input', lambda _='': next(answers))
    monkeypatch.setattr(batch_runner, 'play_to_end', lambda *_a, **_k: 'foreground_required')
    with sync_playwright() as p:
        browser = launch(p)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.set_content('''<title>近代史</title>
          <div class="chapter_item" id="cur11">
            <a class="clicktitle" onclick="document.querySelector('#lesson').innerHTML='<video width=180 height=90></video>'">1.1 视频学习</a>
            <div class="catalog_task"><span class="catalog_points_yi">1</span></div>
          </div><div id="lesson"></div>''')
        helper = Helper(ctx)
        batch_runner.run_batch(helper)
        browser.close()
    report = json.loads((tmp_path / 'batch_report.json').read_text(encoding='utf-8'))
    assert report['course_id'] == 'history'
    assert report['chapters'][0]['outcome'] == 'video_needs_manual_attention'
    state = load_state(tmp_path, 'history')
    assert state['chapters'][0]['last_outcome'] == 'video_needs_manual_attention'
    course_report = json.loads(
        (tmp_path / 'courses' / 'history' / 'batch_report.json').read_text(encoding='utf-8'))
    assert course_report['chapters'][0]['block_reason'] == 'foreground_required'
    log = (tmp_path / 'courses' / 'history' / 'events.log').read_text(encoding='utf-8')
    assert 'video_needs_manual_attention' in log
    assert 'password' not in log


def test_load_targets_reads_config_and_skips_bad_entries(tmp_path):
    from course_tasks import load_targets
    cfg = tmp_path / 'courses.json'
    cfg.write_text(json.dumps([
        {'id': 'a b', 'name': '课程A', 'aliases': ['课程A（2026）']},
        {'name': '缺少 id'},
        'not a dict',
    ], ensure_ascii=False), encoding='utf-8')
    assert load_targets(cfg) == ({'id': 'a_b', 'name': '课程A', 'aliases': ('课程A（2026）',)},)
    assert load_targets(tmp_path / 'missing.json') == ()
