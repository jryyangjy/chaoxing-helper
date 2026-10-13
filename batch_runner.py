"""Best-effort batch navigator for courses whose chapter sidebar is visible.

Records real media playback, never fakes progress or auto-submits assessments.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from playwright.sync_api import Error as PlaywrightError

if TYPE_CHECKING:
    from study_helper import Helper

ROOT = Path(__file__).resolve().parent
SCAN_JS = (ROOT / 'course_scan.js').read_text(encoding='utf-8')


@dataclass
class Chapter:
    signature: str
    label: str
    frame_hint: str
    status: str = 'unknown'
    pending: str = ''


def discover_chapters(page):
    chapters: list[Chapter] = []
    seen = set()
    for f in list(page.frames):
        try:
            entries = f.evaluate(SCAN_JS)
        except PlaywrightError:
            continue
        for item in entries:
            key = (f.url, item['signature'])
            if key in seen:
                continue
            seen.add(key)
            chapters.append(Chapter(item['signature'], item['label'], f.url,
                                    item.get('status','unknown'), item.get('pending','')))
    return chapters


def catalog_page(ctx, preferred):
    """Return a still-open page whose chapter list is actually visible."""
    ordered = []
    if preferred is not None and not preferred.is_closed():
        ordered.append(preferred)
    for page in list(ctx.pages):
        if page not in ordered and not page.is_closed():
            ordered.append(page)
    for page in ordered:
        try:
            if discover_chapters(page):
                return page
        except PlaywrightError:
            continue
    return None


def click_chapter(page, chapter):
    """Rescan to avoid using stale DOM nodes after a chapter re-renders."""
    frames = list(page.frames)
    ordered = [frame for frame in frames if frame.url == chapter.frame_hint]
    ordered.extend(frame for frame in frames if frame not in ordered)
    for frame in ordered:
        try:
            found = frame.evaluate(SCAN_JS)
            match = next((x for x in found if x['signature'] == chapter.signature), None)
            if match:
                frame.locator(f'[data-study-helper-chapter="{match["index"]}"]').first.click(timeout=5000)
                return True
        except PlaywrightError:
            continue
    return False


def _native_play_click(frame, index: int) -> bool:
    """Try an actual visible playback control; do not modify the media element state by JS."""
    frames = [frame]
    try:
        for other in frame.page.frames:
            if other not in frames:
                frames.append(other)
    except PlaywrightError:
        pass
    selectors = ('.vjs-big-play-button', '.vjs-play-control',
                 '.prism-big-play-btn', '.prism-play-btn', '.play-button',
                 'button[aria-label*="播放"]', 'button[title*="播放"]',
                 'button[aria-label*="Play"]')
    for current in frames:
        for selector in selectors:
            try:
                button = current.locator(selector).first
                if button.count() and button.is_visible() and button.is_enabled():
                    button.click(timeout=1200)
                    return True
            except PlaywrightError:
                continue
    try:
        video = frame.locator('video').nth(index)
        if video.count():
            video.scroll_into_view_if_needed(timeout=1000)
            video.click(timeout=1500)
            return True
    except PlaywrightError:
        pass
    return False


def play_to_end(helper: 'Helper', frame, video_index: int, label: str,
                *, poll: float = 2.0, idle_limit: int = 18,
                max_seconds: int = 4*3600, max_retries: int = 3,
                on_progress: Callable[[dict], None] | None = None,
                wait_for_focus: bool = False):
    """Play via native controls, recover transient pauses without spoofing attention.

    Explicitly stop for hidden/unfocused tabs, verification UI or media errors;
    these are not ordinary buffering interruptions. wait_for_focus only waits;
    it never fabricates focus, visibility, or mouse movement.
    """
    from study_helper import VIDEO_JS
    from playback_recovery import pause_reason, is_buffering
    try:
        last = None
        idle = 0
        buffering_polls = 0
        recovery_attempts = 0
        started = time.monotonic()
        last_checkpoint = started - 30
        initial_start = True
        focus_note = 0.0
        raised_tab = False
        while True:
            states = frame.evaluate(VIDEO_JS)
            if video_index >= len(states):
                return 'video_disappeared'
            current = states[video_index]
            position = current['current']
            if time.monotonic() - last_checkpoint >= 15:
                if on_progress:
                    on_progress(current)
                last_checkpoint = time.monotonic()
            if current['ended']:
                print('\n     本地播放器结束（不等于后台任务完成）')
                helper.record_video(frame.url, video_index, current, 'ended_local')
                return 'ended_local'
            if time.monotonic() - started >= max_seconds:
                print('\n     达到单视频安全时限。')
                helper.record_video(frame.url, video_index, current, 'timeout')
                return 'timeout'
            if last is not None and position <= last + 0.25:
                idle += 1
            else:
                idle = 0
                buffering_polls = 0
                # Successful playback resets the transient recovery budget.
                if last is not None and position > last + 2:
                    recovery_attempts = 0
            last = position
            print(f'\r     {label[:45]}：{position:.0f}/{current["duration"] or "?"} 秒',end='',flush=True)
            from popup_quiz import answer_video_popup
            popup = answer_video_popup(helper, frame)
            if popup == 'answered':
                idle = 0
                recovery_attempts = 0
                time.sleep(0.6)
                continue
            if popup == 'blocked':
                helper.record_video(frame.url, video_index, current, 'manual_verification')
                return 'manual_verification'
            if popup == 'open':
                time.sleep(max(poll, 1.0))
                continue
            if current['paused']:
                reason = pause_reason(current)
                # The user just pressed RUN in a terminal; the course page may
                # not have focus until the FIRST native Playwright click.
                # A single initial click is normal navigation. After playback
                # starts, loss of focus is never spoofed/repaired.
                if (initial_start and reason == 'foreground_required'
                    and current.get('visibility') == 'visible'
                    and not current.get('guard_visible')):
                    reason = 'normal_pause'
                if reason != 'normal_pause':
                    if (wait_for_focus and reason == 'foreground_required'
                            and not current.get('guard_visible')):
                        if not raised_tab:
                            try:
                                frame.page.bring_to_front()
                                raised_tab = True
                                initial_start = True
                                print('\n     视频标签不在前台，已切换到该标签一次。')
                                time.sleep(0.6)
                                continue
                            except PlaywrightError:
                                raised_tab = True
                        now = time.monotonic()
                        if now - focus_note >= 30:
                            print('\n     课程窗口不在前台，播放已暂停。鼠标可以去做别的事；'
                                  '回到这个窗口后会继续。不会伪造焦点，也不会模拟鼠标。')
                            focus_note = now
                        time.sleep(max(poll, 1.0))
                        continue
                    print(f'\n     暂停原因：{reason}；需要人工处理，不会模拟前台/鼠标活动。')
                    helper.record_video(frame.url, video_index, current, reason)
                    return reason
                if recovery_attempts >= max_retries:
                    print('\n     正常播放控件尝试恢复多次无效，转人工检查。')
                    helper.record_video(frame.url, video_index, current, 'resume_failed')
                    return 'resume_failed'
                recovery_attempts += 1
                initial_start = False
                print(f'\n     播放意外暂停，尝试正常播放控件（{recovery_attempts}/{max_retries}）')
                if not _native_play_click(frame, video_index):
                    if recovery_attempts >= max_retries:
                        print('\n     没有点到播放控件。')
                        helper.record_video(frame.url, video_index, current, 'no_play_control')
                        return 'no_play_control'
                    time.sleep(max(poll, 0.8))
                    continue
                time.sleep(max(poll, 0.25))
                continue
            initial_start = False
            if idle >= idle_limit:
                if is_buffering(current) and buffering_polls < 20:
                    # Buffering is not a pause. Allow up to ~40 s at default poll.
                    buffering_polls += 1
                else:
                    print('\n     视频没有前进，可能网络停滞或页面要求交互。')
                    helper.record_video(frame.url, video_index, current, 'stalled')
                    return 'stalled'
            time.sleep(max(poll,0.1))
    except (PlaywrightError, IndexError) as exc:
        print(f'\n     视频元素不可用：{str(exc)[:110]}')
        return 'error'


def resolve_course_page(helper: 'Helper'):
    """Find chapter list in the selected tab, then other open tabs (including new user-opened tabs)."""
    selected = helper.page
    if not selected.is_closed():
        chapters = discover_chapters(selected)
        if chapters:
            return selected, chapters
    candidates = []
    for page in helper.ctx.pages:
        if page is selected or page.is_closed():
            continue
        chapters = discover_chapters(page)
        if chapters:
            candidates.append((page, chapters))
    if len(candidates) == 1:
        page, chapters = candidates[0]
        print(f'当前选中的标签页没有章节；自动切换到课程标签：{page.title()[:60]}')
        helper.page = page
        page.bring_to_front()
        return page, chapters
    if len(candidates) > 1:
        print('检测到多个包含章节目录的标签页，请明确选择：')
        for i, (page, chapters) in enumerate(candidates, 1):
            print(f'  [{i}] {page.title()[:52]}（检测到 {len(chapters)} 个章节）')
        choice = input('标签页序号（回车取消）: ').strip()
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            page, chapters = candidates[int(choice)-1]
            helper.page = page
            page.bring_to_front()
            return page, chapters
    return selected, []


def _resume_suggestion(report_path: Path, chapters: list[Chapter], course_url: str,
                       course_id: str | None = None) -> int:
    """Suggest the first unresolved chapter from a prior local report, never restore media time."""
    from study_helper import safe_url
    try:
        prior = json.loads(report_path.read_text(encoding='utf-8'))
        same_course = False
        if course_id and prior.get('course_id') == course_id:
            same_course = True
        elif prior.get('initial_course_page') == safe_url(course_url):
            same_course = True
        if not same_course:
            return 1
        for row in prior.get('chapters', []):
            n = row.get('number')
            if (isinstance(n,int) and 1 <= n <= len(chapters)
                and chapters[n-1].label == row.get('chapter')
                and row.get('outcome') not in
                  ('catalog_marked_complete_skipped','video_ended_verify_platform',
                   'questions_submitted','not_pending_skipped')):
                return n
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return 1


def _course_from_page(page):
    from course_tasks import HINT_JS, match_course
    try:
        title = page.title() or ''
    except PlaywrightError:
        title = ''
    course = match_course(title)
    if course:
        return course
    for frame in list(page.frames):
        try:
            hints = frame.evaluate(HINT_JS) or []
        except PlaywrightError:
            continue
        for hint in hints:
            course = match_course(hint)
            if course:
                return course
    return None


def run_batch(helper: 'Helper', *, preset: dict | None = None, wait_for_focus: bool = False):
    """One-course best effort processing; no invisible completion or uncontrolled submit."""
    from chapter_navigator import choose_loaded_page, page_evidence
    from course_tasks import (
        batch_report_path, format_summary, load_state, note, remember_catalog,
        update_chapter_progress,
    )
    from study_helper import OUTPUT, save_json, safe_url, stamp
    root_page, chapters = resolve_course_page(helper)
    if not chapters:
        print('课程章节未找到。请打开“章节”页，先执行菜单 8 或 D 获取诊断。')
        return
    course = _course_from_page(root_page)
    print(f'识别到 {len(chapters)} 个目录项目（完成标记仅反映当前课程页面）：')
    if course:
        state = remember_catalog(OUTPUT, course, chapters, safe_url(root_page.url), stamp())
        print(f'目标课程：{course["name"]}。{format_summary(state)}')
        print(f'进度文件：output/courses/{course["id"]}/ （与另一门课分开保存）')
        note(OUTPUT, course['id'], f'识别章节 {len(chapters)}，未完成 {state["summary"]["unfinished"]}', stamp())
    for i, ch in enumerate(chapters, 1):
        status = {'complete':'✓已完成', 'pending':f'待完成 {ch.pending}', 'unknown':'状态未知'}[ch.status]
        print(f'  {i:03d} [{status}] {ch.label[:75]}')
    resume_path = batch_report_path(OUTPUT, course['id']) if course else OUTPUT / 'batch_report.json'
    resume = _resume_suggestion(resume_path, chapters, root_page.url,
                                course['id'] if course else None)
    if resume != 1:
        print(f'上次本地报告中第 {resume} 项未处理完；建议从这里继续（不会跳过平台播放验证）。')
    if preset is None:
        ask = input
    else:
        queued = iter((
            preset.get('start', ''),
            preset.get('end', ''),
            preset.get('skip', 'Y'),
            preset.get('mode', '0'),
            preset.get('blocked', 'Y'),
            preset.get('confirm', 'RUN'),
        ))

        def ask(prompt=''):
            value = next(queued)
            print(prompt + value)
            return value
    choice = ask(f'从第几项开始（默认 {resume}）？ ').strip() or str(resume)
    if not choice.isdigit() or not 1 <= int(choice) <= len(chapters):
        print('序号无效')
        return
    first = int(choice)
    last_text = ask(f'处理到第几项结束（默认 {len(chapters)}；首次建议先测一项）？ ').strip()
    if last_text and (not last_text.isdigit() or not first <= int(last_text) <= len(chapters)):
        print('结束序号无效')
        return
    last = int(last_text) if last_text else len(chapters)
    skip = ask('跳过目录中明确标记已完成的项？[Y/n] ').strip().lower() != 'n'
    mode = ask('处理题目：0=仅导出(默认)，1=本地 AI 给出建议，不填写：').strip() or '0'
    if mode not in ('0','1'):
        print('模式不支持。AI 答案必须人工核对后才能填写。')
        return
    continue_blocked = ask('播放限制或未知任务点：记录待处理并继续下一章节？[Y/n] ').strip().lower() != 'n'
    only_pending = bool(preset and preset.get('only_pending'))
    if ask(f'确认开始逐项处理 {first}—{last}，请输入 RUN：').strip() != 'RUN':
        return
    report_path = OUTPUT / 'batch_report.json'
    run_tag = time.strftime('%Y%m%d_%H%M%S')
    archive_path = OUTPUT / ('batch_' + run_tag + '.json')
    questions_archive = OUTPUT / ('questions_' + run_tag + '.json')
    report = {'created':stamp(), 'initial_course_page':safe_url(root_page.url),
              'course_id': course['id'] if course else None,
              'course_name': course['name'] if course else None,
              'notes':'播放器本地结束不代表平台完成；未播放完的章节明确标记待人工处理；题目不会自动提交或未经核对填写',
              'chapters':[]}
    all_questions = []

    def persist_report():
        save_json(report_path, report)
        if not course or not report['chapters']:
            return
        save_json(batch_report_path(OUTPUT, course['id']), report)
        last = report['chapters'][-1]
        if update_chapter_progress(OUTPUT, course['id'], last, stamp()):
            reason = last.get('block_reason') or ''
            note(OUTPUT, course['id'],
                 f'第 {last.get("number")} 节 {last.get("chapter", "")[:40]} => {last.get("outcome")} {reason}',
                 stamp())

    try:
        for index in range(first-1, last):
            ch = chapters[index]
            item = {'number':index+1,'chapter':ch.label,'catalog_status':ch.status,
                    'time':stamp(),'video_results':[],'question_count':0,'outcome':'pending'}
            report['chapters'].append(item)
            print(f'\n[{index+1}/{len(chapters)}] {ch.label}（{ch.status}）')
            if only_pending and ch.status != 'pending':
                item['outcome'] = 'not_pending_skipped'
                persist_report()
                continue
            if ch.status == 'complete' and skip:
                item['outcome'] = 'catalog_marked_complete_skipped'
                persist_report()
                continue
            if root_page.is_closed():
                item['outcome'] = 'catalog_closed'
                break
            helper.page = root_page
            old_tabs = set(helper.ctx.pages)
            before = page_evidence(root_page)
            before_other = {p:page_evidence(p) for p in old_tabs
                            if p is not root_page and not p.is_closed()}
            if not click_chapter(root_page,ch):
                restored = False
                for _ in range(2):
                    try:
                        root_page.go_back(wait_until='domcontentloaded', timeout=10000)
                    except PlaywrightError:
                        break
                    for _wait in range(12):
                        if discover_chapters(root_page):
                            break
                        root_page.wait_for_timeout(400)
                    before = page_evidence(root_page)
                    old_tabs = set(helper.ctx.pages)
                    before_other = {p:page_evidence(p) for p in old_tabs
                                    if p is not root_page and not p.is_closed()}
                    if click_chapter(root_page, ch):
                        restored = True
                        print('  已返回课程目录并打开这一节。')
                        break
                if not restored:
                    item['outcome'] = 'chapter_click_failed'
                    print('  节点已经变化，无法再次找到对应项；停止以免点错章节。')
                    _save_failure_snapshot(helper.ctx, item['outcome'], index+1)
                    break
            navigation = choose_loaded_page(helper.ctx,root_page,old_tabs,before, before_other=before_other)
            item['navigation_state'] = navigation.state
            item['navigation_details'] = navigation.details
            if navigation.page is None:
                item['outcome'] = navigation.state
                if continue_blocked and navigation.state in ('not_loaded', 'popup_blank'):
                    print('  '+navigation.details+'；这项跳过，继续下一项未完成内容。')
                    _save_failure_snapshot(helper.ctx, navigation.state, index+1)
                    persist_report()
                    continue
                print('  '+navigation.details+'；批处理暂停。')
                _save_failure_snapshot(helper.ctx, navigation.state, index+1)
                break
            work_page = navigation.page
            helper.page = work_page
            item['page_url'] = safe_url(work_page.url)
            if navigation.state in ('popup_nonempty_unverified','inline_nonempty_unverified'):
                item['outcome'] = 'needs_manual_structure_check'
                print('  已打开页面，但未找到可验证的任务内容；请检查该章节页面。')
                _save_failure_snapshot(helper.ctx, navigation.state, index+1)
                break
            # Give nested media tags a short window to load; never assume a blank player is completed.
            candidates = []
            for _ in range(8):
                candidates = helper._video_candidates()
                if candidates:
                    break
                work_page.wait_for_timeout(500)
            video_blocked = False
            video_marked_done = False
            for frame in list(work_page.frames):
                try:
                    if frame.locator('.ans-job-finished').count():
                        video_marked_done = True
                        break
                except PlaywrightError:
                    continue
            if video_marked_done:
                print('  视频任务点已完成，不再重播。')
                candidates = []
            for frame, vi, video in candidates:
                if not video['visible']:
                    continue
                def checkpoint(v):
                    item['latest_video_progress'] = {
                        'seconds': round(v['current'],1), 'duration': v['duration'],
                        'paused': v['paused'], 'time':stamp()}
                    persist_report()
                status = play_to_end(helper,frame,vi,ch.label,on_progress=checkpoint,
                                     wait_for_focus=wait_for_focus)
                item['video_results'].append({'frame':safe_url(frame.url),'index':vi,'status':status})
                if status not in ('ended_local','already_ended_local'):
                    video_blocked = True
                    item['outcome'] = 'video_needs_manual_attention'
                    item['block_reason'] = status
                    _save_failure_snapshot(helper.ctx, status, index+1)
                    break
            if video_blocked:
                print('  该视频保留为待处理，不会标记已完成。继续提取可访问的章节题目。')
            try:
                quiz_tab = work_page.get_by_text('章节测验', exact=True).first
                if quiz_tab.count() and quiz_tab.is_visible():
                    quiz_tab.click(timeout=4000)
                    work_page.wait_for_timeout(1500)
                    print('  已打开章节测验。')
            except PlaywrightError:
                pass
            helper.extract_questions()
            item['question_count'] = len(helper.questions)
            submitted = False
            if only_pending and helper.questions:
                from popup_quiz import answer_loaded_questions
                submitted = answer_loaded_questions(helper)
            if helper.questions:
                if mode == '1':
                    try:
                        helper.ollama_suggest(confirm=False)
                    except Exception as exc:
                        print('  本地模型不可用：'+type(exc).__name__)
                        item['outcome'] = 'ai_unavailable'
                for qi,q in enumerate(helper.questions,1):
                    all_questions.append({'chapter':ch.label,'chapter_number':index+1,
                        'question_number':qi,'page_url':safe_url(work_page.url),
                        **q.data,'suggestion':q.answer})
                if item['outcome'] == 'pending':
                    item['outcome'] = 'draft_saved' if submitted else 'questions_need_review'
            elif candidates:
                if item['outcome'] == 'pending':
                    item['outcome'] = 'video_ended_verify_platform'
            else:
                item['outcome'] = 'unrecognized_task_requires_review'
                print('  未发现可识别的视频/题目；不视为完成。')
                _save_failure_snapshot(helper.ctx, item['outcome'], index+1)
                if not continue_blocked:
                    break
            save_json(OUTPUT / 'questions_all.json',{'created':stamp(),'questions':all_questions})
            persist_report()
            # Only close *new* lesson windows, never an existing user-owned tab.
            if work_page is not root_page and work_page not in old_tabs:
                try:
                    work_page.close()
                except PlaywrightError:
                    pass
                helper.page = root_page
            elif root_page.url != before['url']:
                try:
                    # The study page often keeps the chapter list. Going back
                    # from there drops the only catalog and stops the course.
                    held = catalog_page(helper.ctx, root_page)
                    if held is not None:
                        root_page = held
                        helper.page = root_page
                    else:
                        root_page.go_back(wait_until='domcontentloaded',timeout=12000)
                        for _ in range(20):
                            held = catalog_page(helper.ctx, root_page)
                            if held is not None:
                                root_page = held
                                helper.page = root_page
                                break
                            root_page.wait_for_timeout(500)
                        else:
                            item['outcome'] = 'catalog_navigation_lost'
                            print('  这一节做完后目录暂时找不到，继续下一项。')
                            _save_failure_snapshot(helper.ctx, item['outcome'], index+1)
                except PlaywrightError:
                    item['outcome'] = 'catalog_navigation_lost'
                    print('  返回目录失败，继续下一项。')
                    _save_failure_snapshot(helper.ctx, item['outcome'], index+1)
            if item['outcome'] == 'draft_saved':
                print('  章节题已暂时保存。正式提交留给你，脚本不会点击提交或交卷。')
            elif item['outcome'] == 'questions_submitted':
                print('  章节题已提交。平台是否记完成，以目录标记为准。')
            elif item['outcome'] == 'questions_need_review':
                print('  题目已导出供核对；未填写/提交。继续下一节。')
            if video_blocked and not continue_blocked:
                print('  根据设置：停止批处理。')
                break
    except KeyboardInterrupt:
        print('\n已由用户中止，保留现有报告。')
        if course:
            note(OUTPUT, course['id'], '用户中止，已保留断点', stamp())
    except Exception as exc:
        if course:
            note(OUTPUT, course['id'], f'异常中断，已保留断点 {type(exc).__name__}', stamp())
        raise
    finally:
        save_json(report_path,report)
        if course:
            save_json(batch_report_path(OUTPUT, course['id']), report)
        save_json(archive_path,report)
        question_dump = {'created':stamp(),'questions':all_questions}
        save_json(questions_archive, question_dump)
        save_json(OUTPUT / 'questions_all.json', question_dump)
        if course:
            try:
                save_json(batch_report_path(OUTPUT, course['id']).with_name('questions_all.json'), question_dump)
                page_url = safe_url(root_page.url) if not root_page.is_closed() else report.get('initial_course_page', '')
                remember_catalog(OUTPUT, course, chapters, page_url, stamp())
                for row in report['chapters']:
                    if update_chapter_progress(OUTPUT, course['id'], row, stamp()):
                        note(OUTPUT, course['id'],
                             f'第 {row.get("number")} 节 => {row.get("outcome")}', stamp())
                note(OUTPUT, course['id'], format_summary(load_state(OUTPUT, course['id'])), stamp())
            except (PlaywrightError, OSError, TypeError) as exc:
                print(f'课程进度写入失败：{type(exc).__name__}')
        print('报告已保存：output/batch_report.json；不是平台成绩/进度的证明。')


def _open_course_entry(helper, frame, item, course):
    """Native click on a course card, then wait until that course's chapter list appears."""
    page = frame.page
    before_pages = {p for p in helper.ctx.pages if not p.is_closed()}
    before_counts = {p: len(discover_chapters(p)) for p in before_pages}
    frame.locator(f'[data-study-helper-course="{item["index"]}"]').first.click(timeout=6000)
    deadline = time.time() + 20
    while time.time() < deadline:
        for current in list(helper.ctx.pages):
            if current.is_closed():
                continue
            titled = _course_from_page(current)
            if titled and titled['id'] != course['id']:
                continue
            chapters = discover_chapters(current)
            if not chapters:
                continue
            opened = current not in before_pages or len(chapters) > before_counts.get(current, 0)
            if opened or (titled and titled['id'] == course['id']):
                return current, chapters
        try:
            page.wait_for_timeout(400)
        except PlaywrightError:
            return None, []
    return None, []


def _login_wall(page) -> bool:
    from chapter_navigator import page_evidence
    try:
        url = page.url or ''
    except PlaywrightError:
        return False
    if 'passport2.chaoxing.com' in url or url.rstrip('/').endswith('/login'):
        return True
    try:
        return bool(page_evidence(page).get('login'))
    except (PlaywrightError, TypeError, KeyError):
        return False


def _frame_text(frame) -> str:
    try:
        return frame.evaluate("() => (document.body && document.body.innerText || '').slice(0, 2500)") or ''
    except PlaywrightError:
        return ''


def _find_course_entries(helper, seen: set[str]):
    from course_tasks import FIND_JS, match_course, search_names
    entries = []
    claimed: set[str] = set()
    names = search_names()
    for page in list(helper.ctx.pages):
        if page.is_closed():
            continue
        for frame in list(page.frames):
            try:
                found = frame.evaluate(FIND_JS, names) or []
            except PlaywrightError:
                continue
            for item in found:
                course = match_course(item.get('course_name') or item.get('label') or '')
                if not course or course['id'] in seen or course['id'] in claimed:
                    continue
                claimed.add(course['id'])
                entries.append((frame, course, item))
    return entries


def _click_named_tab(helper, labels: tuple[str, ...]) -> bool:
    script = '''(labels) => {
      const nodes = [...document.querySelectorAll('a, button, [role="tab"], span, li, div')];
      for (const el of nodes) {
        if (el.children.length > 3) continue;
        const raw = (el.innerText || '').replace(/\\s+/g, ' ').trim();
        if (!labels.includes(raw)) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width < 2 || rect.height < 2 || rect.height > 80 || rect.width > 220) continue;
        el.setAttribute('data-study-helper-tab', '1');
        return true;
      }
      return false;
    }'''
    for page in list(helper.ctx.pages):
        if page.is_closed() or _login_wall(page):
            continue
        for frame in list(page.frames):
            try:
                if not frame.evaluate(script, list(labels)):
                    continue
                frame.locator('[data-study-helper-tab="1"]').first.click(timeout=4000)
                page.wait_for_timeout(800)
                return True
            except PlaywrightError:
                continue
    return False


def _course_card_labels(helper) -> list[str]:
    label_js = r'''() => {
      const sel = '.course-name,.courseName,.course_name,.course-title,.studyCard .name,.courseItem h3';
      const out = [];
      const seen = new Set();
      for (const el of document.querySelectorAll(sel)) {
        const raw = (el.getAttribute('title') || (el.innerText || '').split('\n')[0] || '').replace(/\s+/g, ' ').trim();
        if (raw.length < 2 || raw.length > 40 || seen.has(raw)) continue;
        seen.add(raw);
        out.push(raw);
        if (out.length >= 30) break;
      }
      return out;
    }'''
    labels = []
    seen = set()
    for page in list(helper.ctx.pages):
        if page.is_closed():
            continue
        for frame in list(page.frames):
            try:
                found = frame.evaluate(label_js) or []
            except PlaywrightError:
                continue
            for label in found:
                if label not in seen:
                    seen.add(label)
                    labels.append(label)
    return labels


def _print_portal_clues(helper):
    """Print page titles and short course labels. No cookies, query strings, or passwords."""
    from study_helper import safe_url
    label_js = r'''() => {
      const sel = '.course-name,.courseName,.course_name,.course-title,h3,h4,.studyCard .name,.courseItem';
      const out = [];
      const seen = new Set();
      for (const el of document.querySelectorAll(sel)) {
        const raw = (el.getAttribute('title') || (el.innerText || '').split('\n')[0] || '').replace(/\s+/g, ' ').trim();
        if (raw.length < 2 || raw.length > 40 || seen.has(raw)) continue;
        seen.add(raw);
        out.push(raw);
        if (out.length >= 30) break;
      }
      return out;
    }'''
    print('当前页面：' + _wait_briefs(helper))
    shown = False
    for page in list(helper.ctx.pages):
        if page.is_closed():
            continue
        for frame in list(page.frames):
            try:
                labels = frame.evaluate(label_js) or []
                frame_url = safe_url(frame.url)
            except PlaywrightError:
                continue
            if not labels:
                continue
            shown = True
            print(f'  {frame_url[:90]}：' + '、'.join(labels[:12]))
    if not shown:
        print('  页面上还没有读到课程卡片。请在浏览器里打开「课程」列表，或点进其中一门课的章节页。')


def _portal_ready(helper) -> bool:
    """True when some open page is past login and shows the course home or a target course."""
    from course_tasks import FIND_JS, search_names
    pages = [p for p in helper.ctx.pages if not p.is_closed()]
    if not pages:
        return False
    names = search_names()
    markers = ('退出', '我的课程', '我学的课', *names)
    for page in pages:
        if _login_wall(page):
            continue
        if _course_from_page(page) and discover_chapters(page):
            return True
        try:
            url = page.url or ''
        except PlaywrightError:
            continue
        if 'passport2.chaoxing.com' in url:
            continue
        for frame in list(page.frames):
            frame_url = ''
            try:
                frame_url = frame.url or ''
            except PlaywrightError:
                continue
            if 'passport2.chaoxing.com' in frame_url:
                continue
            try:
                if frame.evaluate(FIND_JS, names) or []:
                    return True
            except PlaywrightError:
                pass
            text = _frame_text(frame)
            if any(token in text for token in markers):
                return True
            if any(part in frame_url for part in ('/base', '/space', 'mycourse', 'studentcourse')):
                return True
        if 'chaoxing.com' in url and any(part in url for part in ('/base', '/space', 'mycourse', 'studentcourse')):
            return True
    return False


def _wait_briefs(helper) -> str:
    from study_helper import safe_url
    parts = []
    for page in helper.ctx.pages:
        if page.is_closed():
            continue
        try:
            parts.append(f'{safe_url(page.url)[:90]} | {(page.title() or "")[:40]}')
        except PlaywrightError:
            parts.append('unreadable')
    return ' ; '.join(parts[:6])


def survey_target_courses(helper: 'Helper', *, interactive: bool = True):
    """Read the catalogs of courses in courses.json, store separate state, process unfinished first.

    Does not play media or submit answers unless the user continues into run_batch.
    """
    from course_tasks import (
        FIND_JS, TARGETS, format_summary, match_course, note, prioritize, remember_catalog,
    )
    from study_helper import OUTPUT, safe_url, stamp

    if not TARGETS:
        print('没有配置课程。请把 courses.example.json 复制为 courses.json，并填写你的课程名称。')
        return []
    if interactive:
        pages = [p for p in helper.ctx.pages if not p.is_closed()]
        if any(_login_wall(p) for p in pages):
            print('检测到登录页。请在浏览器窗口手动登录，脚本不会读取或保存密码。')
            input('登录完成后按 Enter 继续扫描…')
    else:
        print('请在 Chrome 窗口中自行登录。账号密码只留在浏览器里，不要发到聊天或脚本。')
        deadline = time.time() + 180
        ready = False
        ticks = 0
        while time.time() < deadline:
            if _portal_ready(helper):
                ready = True
                break
            if ticks % 4 == 0:
                print('等待课程页：' + _wait_briefs(helper))
            ticks += 1
            time.sleep(2)
        if not ready:
            print('等待登录超时。请在浏览器完成登录后，重新运行菜单 E。')
            return []

    snapshots = []
    seen: set[str] = set()

    def absorb(page, course, chapters):
        if not chapters or course['id'] in seen:
            return
        state = remember_catalog(OUTPUT, course, chapters, safe_url(page.url), stamp())
        note(OUTPUT, course['id'], f'扫描目录，未完成 {state["summary"]["unfinished"]}', stamp())
        snapshots.append({'course': course, 'page': page, 'chapters': chapters, 'state': state})
        seen.add(course['id'])

    for page in list(helper.ctx.pages):
        if page.is_closed():
            continue
        course = _course_from_page(page)
        if course:
            absorb(page, course, discover_chapters(page))

    missing = [course for course in TARGETS if course['id'] not in seen]
    if missing:
        print('尚未打开章节目录：' + '、'.join(course['name'] for course in missing))
        entries = []
        attempts = 12 if not interactive else 1
        for _ in range(attempts):
            entries = _find_course_entries(helper, seen)
            if entries or interactive:
                break
            time.sleep(1)
        if not entries and not interactive:
            if _click_named_tab(helper, ('课程', '我学的课')):
                print('已点击「课程」入口，继续查找这两门课。')
                for _ in range(10):
                    entries = _find_course_entries(helper, seen)
                    if entries:
                        break
                    time.sleep(1)
        if not entries:
            _print_portal_clues(helper)
            print('没有在已打开页面找到这两门课的入口。请手动打开课程的「章节」页。')
        else:
            cards = _course_card_labels(helper)
            if cards:
                print('页面课程卡片：' + '、'.join(cards[:25]))
            print('在当前页面找到课程入口：' + '、'.join(course['name'] for _, course, _ in entries))
            open_them = True
            if interactive:
                open_them = input('打开这些课程并只读取章节目录？[Y/n] ').strip().lower() != 'n'
            if open_them:
                for frame, course, item in entries:
                    if course['id'] in seen:
                        continue
                    try:
                        opened, chapters = _open_course_entry(helper, frame, item, course)
                    except PlaywrightError as exc:
                        print(f'  打开「{course["name"]}」失败：{str(exc)[:120]}')
                        continue
                    if opened and chapters:
                        absorb(opened, course, chapters)
                        print(f'  已读取「{course["name"]}」目录，共 {len(chapters)} 节。')
                    else:
                        print(f'  「{course["name"]}」入口已点击，但没有识别到章节目录。请手动打开章节页后重新扫描。')

    if not snapshots:
        print('没有可汇总的目标课程。')
        return []
    ranked = prioritize(snapshots)
    print('\n===== 目标课程进度（未完成优先）=====')
    for snap in ranked:
        print(format_summary(snap['state']))
        unfinished = [row for row in snap['state']['chapters'] if row.get('catalog_status') != 'complete']
        for row in unfinished[:15]:
            extra = row.get('last_outcome') or '尚未处理'
            print(f'    {row["number"]:03d} [{row.get("catalog_status")}] {row["label"][:50]} | {extra}')
        if len(unfinished) > 15:
            print(f'    …还有 {len(unfinished) - 15} 节未完成')
    top = ranked[0]
    if interactive and top['state']['summary']['unfinished']:
        agreed = input(f'现在处理未完成更多的「{top["course"]["name"]}」？[Y/n] ').strip().lower() != 'n'
        if agreed:
            helper.page = top['page']
            try:
                top['page'].bring_to_front()
            except PlaywrightError:
                pass
            run_batch(helper)
    elif not interactive:
        print('本次只扫描目录并保存进度，不自动播放、不填写、不提交。')
    return ranked


def place_course_window(page):
    """Keep the course window in a corner. Does not steal focus again later."""
    try:
        client = page.context.new_cdp_session(page)
        info = client.send('Browser.getWindowForTarget')
        client.send('Browser.setWindowBounds', {
            'windowId': info['windowId'],
            'bounds': {'left': 12, 'top': 12, 'width': 880, 'height': 560, 'windowState': 'normal'},
        })
        return True
    except (PlaywrightError, KeyError, TypeError):
        return False


def _arm_submit_dialogs(ctx):
    """Accept the browser confirm that follows 提交. Dismiss 交卷 and verification prompts."""
    def handle(dialog):
        text = dialog.message or ''
        if '交卷' in text or '验证' in text or '人脸' in text:
            dialog.dismiss()
            return
        if '提交' in text or '保存' in text:
            dialog.accept()
            return
        dialog.dismiss()

    def arm(page):
        try:
            page.on('dialog', handle)
        except PlaywrightError:
            pass

    for page in list(ctx.pages):
        arm(page)
    try:
        ctx.on('page', arm)
    except PlaywrightError:
        pass


def run_pending_courses(helper: 'Helper'):
    """Play unfinished chapters of both target courses without faking attention.

    Polling does not move the mouse or pull the window forward. If the course
    pauses because the window is in the background, playback waits.
    """
    _arm_submit_dialogs(helper.ctx)
    print('课程窗口会留在屏幕左上角。播放期间脚本只读取进度，不移动鼠标，也不反复把窗口拉到最前。')
    print('你可以用鼠标做别的事。如果课程因为窗口不在前台而暂停，会停在这一节等待；不会伪造焦点或模拟鼠标。')
    if not place_course_window(helper.page):
        print('未能调整窗口位置，继续使用当前窗口。')
    try:
        helper.page.goto('https://i.chaoxing.com/base', wait_until='domcontentloaded', timeout=30000)
    except PlaywrightError:
        print('未能打开课程主页，改为查看当前已打开的页面。')
    ranked = survey_target_courses(helper, interactive=False)
    if not ranked:
        return
    only = {x.strip() for x in os.environ.get('CHAOXING_ONLY_COURSES', '').split(',') if x.strip()}
    if only:
        ranked = [snap for snap in ranked if snap['course']['id'] in only]
    preset = {'start': '', 'end': '', 'skip': 'Y', 'mode': '0', 'blocked': 'Y',
              'confirm': 'RUN', 'only_pending': 'Y'}
    for snap in ranked:
        pending = snap['state']['summary'].get('pending', 0)
        if pending <= 0:
            print(f'{snap["course"]["name"]} 没有目录标成待完成的项目，跳过。')
            continue
        numbers = [row['number'] for row in snap['state']['chapters']
                   if row.get('catalog_status') == 'pending' and isinstance(row.get('number'), int)]
        run_preset = dict(preset)
        if numbers:
            run_preset['start'] = str(min(numbers))
        print(f'\n开始处理「{snap["course"]["name"]}」里标记为待完成的项目。每节只处理未完成的视频和题目。')
        print('视频中弹出的题目会直接选择并提交；章节测验只暂时保存，不点击提交。验证码仍交给你本人。不交卷。')
        helper.page = snap['page']
        place_course_window(snap['page'])
        run_batch(helper, preset=run_preset, wait_for_focus=True)


def _save_failure_snapshot(ctx, state: str, chapter_index: int):
    """No credentials, query parameters, raw question text or cookies."""
    from study_helper import OUTPUT, save_json, stamp, safe_url
    from chapter_navigator import page_evidence
    log = {'time':stamp(), 'reason':state, 'chapter_number':chapter_index, 'tabs':[]}
    for p in ctx.pages:
        if p.is_closed():
            continue
        e = page_evidence(p)
        log['tabs'].append({'url':safe_url(p.url), 'content':e['content'],
            'login_detected':e['login'], 'body_length':e['body_length'],
            'frames':[{'url':safe_url(url),'counts':doc.get('counts',{}),
                       'ready':doc.get('ready'),'body_length':doc.get('bodyLength')}
                      for url,doc in e['frames']]})
    save_json(OUTPUT / 'last_failure.json', log)
    print('  已自动保存 output/last_failure.json（分享前仍请检查隐私信息）。')
