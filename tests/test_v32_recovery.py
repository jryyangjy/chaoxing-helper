"""Behavioral tests for v3.2: transient pauses, hard restrictions, checkpoints."""
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import pytest
from playwright.sync_api import sync_playwright
from batch_runner import play_to_end
from study_helper import Helper
from playback_recovery import pause_reason, is_buffering


def fixture_page(ctx, html):
    page = ctx.new_page()
    page.set_content(html)
    return page


def media_src():
    fixture = Path(__file__).parent / 'fixtures' / 'sample.webm'
    return 'data:video/webm;base64,' + base64.b64encode(fixture.read_bytes()).decode()


def launch(p):
    import shutil
    return p.chromium.launch(headless=True, **({'executable_path':shutil.which('chromium')} if shutil.which('chromium') else {}))


def test_normal_pause_is_recoverable():
    assert pause_reason({'paused':True,'visibility':'visible','focused':True})=='normal_pause'


def test_foreground_and_verification_are_not_bypassed():
    assert pause_reason({'visibility':'hidden','focused':True})=='foreground_required'
    assert pause_reason({'visibility':'visible','focused':False})=='foreground_required'
    assert pause_reason({'visibility':'visible','focused':True,'guard_visible':True})=='manual_verification'
    assert pause_reason({'visibility':'visible','focused':True,'error_code':3})=='media_error'


def test_buffering_classification():
    assert is_buffering({'paused':False,'ended':False,'ready_state':2})
    assert not is_buffering({'paused':True,'ready_state':2})


def test_transient_pause_resumes_via_native_button():
    with sync_playwright() as p:
        browser=launch(p);ctx=browser.new_context()
        page=fixture_page(ctx, f'''<video muted width=160 height=90 src="{media_src()}"></video>
            <button class="play-button" onclick="document.querySelector('video').play()">播放</button>''')
        helper=Helper(ctx)
        history=[]
        helper.record_video=lambda *args: history.append(args)
        events=[]
        page.evaluate("""() => {
            let pausedOnce=false;
            let v=document.querySelector('video');
            v.addEventListener('timeupdate',()=>{
                if(!pausedOnce && v.currentTime>.35){pausedOnce=true;v.pause();}
            });
        }""")
        res=play_to_end(helper,page.main_frame,0,'短视频',poll=.18,idle_limit=6,
                        max_seconds=8,on_progress=lambda s:events.append(s['current']))
        assert res=='ended_local', history
        assert page.locator('video').evaluate('(v)=>v.ended')
        assert len(events)>=1
        browser.close()


def test_hidden_focus_is_not_restarted():
    with sync_playwright() as p:
        browser=launch(p);ctx=browser.new_context()
        page=fixture_page(ctx, f'''<video muted width=160 height=90 src="{media_src()}"></video>
            <button class="play-button" onclick="document.querySelector('video').play()">播放</button>''')
        helper=Helper(ctx)
        history=[]
        helper.record_video=lambda *args:history.append(args)
        # A second tab makes the original page hidden in Chromium.
        ctx.new_page().set_content('<p>other tab</p>')
        # Chromium may still report hasFocus() in headless mode; both should be false/hidden.
        state=page.main_frame.evaluate('() => ({vis:document.visibilityState, focus:document.hasFocus()})')
        if state['vis']=='hidden' or state['focus'] is False:
            res=play_to_end(helper,page.main_frame,0,'后台',poll=.1,max_seconds=3)
            assert res=='foreground_required'
            assert history[0][3]=='foreground_required'
        else:
            browser.close()
            pytest.skip('Browser does not expose background focus in this headless setup')
        browser.close()


def test_verification_overlay_requires_user():
    with sync_playwright() as p:
        browser=launch(p);ctx=browser.new_context()
        page=fixture_page(ctx, f'''<video muted width=160 height=90 src="{media_src()}"></video>
            <button class="play-button">播放</button>
            <div role="dialog" style="width:150px;height:70px">验证码：请人工确认</div>''')
        helper=Helper(ctx)
        history=[]
        helper.record_video=lambda *args:history.append(args)
        res=play_to_end(helper,page.main_frame,0,'有人工验证',poll=.1,max_seconds=3)
        assert res=='manual_verification'
        assert history[0][3]=='manual_verification'
        browser.close()


def test_resume_suggestion_from_unfinished_48(tmp_path):
    from batch_runner import Chapter, _resume_suggestion
    report=tmp_path/'batch_report.json'
    chapters=[Chapter(str(i),str(i),'about:blank') for i in range(1,50)]
    report.write_text(json.dumps({
       'initial_course_page':'https://example.org/course',
       'chapters':[{'number':48,'chapter':'48',
          'outcome':'needs_interaction'}]}),encoding='utf8')
    assert _resume_suggestion(report,chapters,'https://example.org/course?token=SECRET')==48
    assert _resume_suggestion(report,chapters,'https://elsewhere.org/course')==1


def test_batch_does_not_overwrite_blocked_video_status(tmp_path, monkeypatch):
    import batch_runner, study_helper
    monkeypatch.setattr(study_helper,'OUTPUT',tmp_path)
    responses=iter(['1','1','Y','0','Y','RUN'])
    monkeypatch.setattr('builtins.input',lambda _='':next(responses))
    monkeypatch.setattr(batch_runner,'play_to_end',lambda *_a,**_k:'foreground_required')
    with sync_playwright() as p:
        browser=launch(p);ctx=browser.new_context();page=ctx.new_page()
        page.set_content('''<div class="chapter_item" id="cur11">
          <a class="clicktitle" onclick="document.querySelector('#lesson').innerHTML='<video width=180 height=90></video>'">1.1 视频学习</a>
        </div><div id="lesson"></div>''')
        helper=Helper(ctx)
        batch_runner.run_batch(helper)
        report=json.loads((tmp_path/'batch_report.json').read_text(encoding='utf8'))
        row=report['chapters'][0]
        assert row['outcome']=='video_needs_manual_attention'
        assert row['block_reason']=='foreground_required'
        assert row['video_results'][0]['status']=='foreground_required'
        browser.close()


def test_batch_can_continue_scanning_after_unfinished_video(tmp_path, monkeypatch):
    import batch_runner, study_helper
    monkeypatch.setattr(study_helper,'OUTPUT',tmp_path)
    responses=iter(['1','2','Y','0','Y','RUN'])
    monkeypatch.setattr('builtins.input',lambda _='':next(responses))
    monkeypatch.setattr(batch_runner,'play_to_end',lambda *_a,**_k:'foreground_required')
    with sync_playwright() as p:
        browser=launch(p);ctx=browser.new_context();page=ctx.new_page()
        page.set_content('''<div class="chapter_item" id="cur11">
          <a class="clicktitle" onclick="document.querySelector('#lesson').innerHTML='<video width=180 height=90 src=/one.webm></video>'">1.1 视频学习</a></div>
          <div class="chapter_item" id="cur12">
          <a class="clicktitle" onclick="document.querySelector('#lesson').innerHTML='<video width=180 height=90 src=/two.webm></video>'">1.2 视频学习</a></div>
          <div id="lesson"></div>''')
        helper=Helper(ctx)
        batch_runner.run_batch(helper)
        rows=json.loads((tmp_path/'batch_report.json').read_text(encoding='utf8'))['chapters']
        assert [x['chapter'] for x in rows]==['1.1 视频学习','1.2 视频学习']
        assert [x['outcome'] for x in rows]==['video_needs_manual_attention']*2
        browser.close()


def test_first_native_start_works_if_terminal_has_focus():
    """RUN is pressed in a terminal; a single browser UI click can start playback."""
    with sync_playwright() as p:
        browser=launch(p);ctx=browser.new_context()
        page=fixture_page(ctx,f'''<video muted width=160 height=90 src="{media_src()}"></video>
          <button class="play-button" onclick="document.querySelector('video').play()">播放</button>''')
        # Simulate focus belonging to the initiating terminal, while the tab is visible.
        page.evaluate("() => Object.defineProperty(document,'hasFocus',{configurable:true,value:()=>false})")
        helper=Helper(ctx)
        helper.record_video=lambda *args:None
        res=play_to_end(helper,page.main_frame,0,'终端触发',poll=.15,max_seconds=8)
        assert res=='ended_local'
        browser.close()
