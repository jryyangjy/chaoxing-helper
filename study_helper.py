#!/usr/bin/env python3
"""Chaoxing Study Helper: navigation, real playback monitoring, quiz extraction and review.

No fabricated video progress, no anti-bot bypass, no unattended exam submission.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from typing import Any

from playwright.sync_api import BrowserContext, Frame, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / 'output'
PROFILE = ROOT / '.browser_profile'
EXTRACTOR = (ROOT / 'quiz_extract.js').read_text(encoding='utf-8')
START_URL = 'https://i.chaoxing.com/'
VIDEO_JS = r'''() => [...document.querySelectorAll('video')].map((v,i) => {
  // Only inspect visible verification overlays, not page-wide course content.
  const overlays = [...document.querySelectorAll(
    '[role="dialog"], .layui-layer, .modal, .vjs-modal-dialog, .verify-dialog, .captcha, .verify-box')];
  const guard = overlays.some(el => {
    const r = el.getBoundingClientRect();
    if (r.width < 10 || r.height < 10 || getComputedStyle(el).visibility === 'hidden') return false;
    const message = (el.innerText || '').slice(0,500);
    return /验证码|验证身份|请人工|人脸|手动确认|滑动验证|继续学习|请点击继续|人机验证/.test(message);
  });
  const ranges = v.buffered;
  let ahead = 0;
  try {
    for (let k=0;k<ranges.length;k++) if (v.currentTime >= ranges.start(k) && v.currentTime <= ranges.end(k)) {
      ahead = Math.max(0,ranges.end(k)-v.currentTime); break;
    }
  } catch(e) {}
  return {
    index:i, current:Number(v.currentTime || 0), duration:Number.isFinite(v.duration)?v.duration:null,
    paused:v.paused, ended:v.ended, muted:v.muted, rate:v.playbackRate,
    visible:!!(v.offsetWidth || v.offsetHeight || v.getClientRects().length),
    ready_state:v.readyState, network_state:v.networkState, buffered_ahead:ahead,
    error_code:v.error ? v.error.code : null,
    visibility:document.visibilityState, focused:document.hasFocus(), guard_visible:guard
  };
})'''
LINK_JS = '''() => {
 const matches = [...document.querySelectorAll('a, [role="link"], [onclick*="chapter"], [onclick*="toOld"], [onclick*="course"]')];
 const result = [];
 for (const el of matches) {
   const label = (el.innerText || el.getAttribute('title') || '').replace(/\\s+/g,' ').trim();
   const href = el.getAttribute('href') || '';
   const click = el.getAttribute('onclick') || '';
   if (!label || label.length > 150) continue;
   if (!/课程|章节|学习|课时|任务|测试|作业|目录|course|chapter|knowledge/i.test(label+' '+href+' '+click)) continue;
   el.setAttribute('data-study-helper-link', String(result.length));
   result.push({ id:result.length, label, href:href.slice(0,200) });
   if (result.length >= 120) break;
 }
 return result;
}'''


def stamp() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def safe_url(url: str) -> str:
    """Diagnostic URL without query-string tokens or fragments."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def clean_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text)
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', text, re.S)
        if not m:
            raise ValueError('AI 未返回 JSON')
        result = json.loads(m.group(0))
    if not isinstance(result, dict):
        raise ValueError('AI 返回不是对象')
    return result


def validate_suggestion(q: dict, response: dict) -> dict:
    choices = {x['key'] for x in q['options']}
    answer = response.get('answer', [])
    if isinstance(answer, str):
        answer = [answer]
    answer = [str(x).strip() if q['type'] == 'blank' else str(x).strip().upper()
              for x in answer] if isinstance(answer, list) else []
    if q['type'] in ('single', 'true_false') and len(answer) != 1:
        raise ValueError('单选/判断需要一个选项')
    if q['type'] == 'multiple' and not answer:
        raise ValueError('多选至少一个选项')
    if q['type'] != 'blank' and (not choices or any(a not in choices for a in answer)):
        raise ValueError(f'答案 {answer} 不匹配页面选项 {sorted(choices)}')
    if q['type'] == 'blank' and len(answer) < 1:
        raise ValueError('没有填空建议')
    c = response.get('confidence', 0)
    try:
        c = max(0.0, min(1.0, float(c)))
    except (TypeError, ValueError):
        c = 0.0
    return {'answer': answer, 'explanation': str(response.get('explanation', ''))[:2000], 'confidence': c}


@dataclass
class LocatedQuestion:
    page: Page
    frame: Frame
    data: dict
    page_number: int
    frame_number: int
    answer: dict | None = None


def frames_of(page: Page) -> list[Frame]:
    return list(page.frames)


class Helper:
    def __init__(self, ctx: BrowserContext):
        self.ctx = ctx
        self.page: Page = ctx.pages[-1] if ctx.pages else ctx.new_page()
        self.questions: list[LocatedQuestion] = []
        self.links: list[tuple[Frame, int, dict]] = []

    def select_page(self):
        pages = [p for p in self.ctx.pages if not p.is_closed()]
        for i, p in enumerate(pages):
            print(f'  [{i}] {p.title()[:45]} | {safe_url(p.url)[:130]}')
        n = input('切换到哪个浏览器标签？输入序号: ').strip()
        if not n.isdigit() or int(n) >= len(pages):
            print('序号无效')
            return
        self.page = pages[int(n)]
        self.page.bring_to_front()

    def info(self):
        print(f'\n当前页面: {self.page.title()}\n{self.page.url}')
        for i, f in enumerate(frames_of(self.page)):
            try:
                vids = f.evaluate(VIDEO_JS)
                indicators = f.locator('.TiMu, .questionLi, .mark_item').count()
                print(f'  frame {i:02d}: videos={len(vids)}, question_blocks={indicators} | {safe_url(f.url)[:105]}')
                for v in vids:
                    dur = f'{v["duration"]:.1f}' if v['duration'] is not None else '?'
                    print(f'     video {v["index"]}: {v["current"]:.1f}s / {dur}s, paused={v["paused"]}, ended={v["ended"]}')
            except (PlaywrightError, ValueError):
                print(f'  frame {i:02d}: 尚未加载/不支持读取 | {safe_url(f.url)[:105]}')

    def discover_links(self):
        self.links.clear()
        for fi, f in enumerate(frames_of(self.page)):
            try:
                data = f.evaluate(LINK_JS)
                for item in data:
                    self.links.append((f, fi, item))
            except PlaywrightError:
                pass
        if not self.links:
            print('没找到课程/章节入口，请在浏览器里手动打开课程目录。')
            return
        print(f'找到 {len(self.links)} 个可能的入口：')
        for i, (_, fi, x) in enumerate(self.links[:100]):
            print(f'  [{i:03d}] frame={fi} {x["label"][:70]}')
        choice = input('点击入口的序号（回车仅查看）: ').strip()
        if not choice:
            return
        if not choice.isdigit() or int(choice) >= len(self.links):
            print('序号无效')
            return
        f, _, el = self.links[int(choice)]
        try:
            f.locator(f'[data-study-helper-link="{el["id"]}"]').first.click(timeout=6000)
            self.page.wait_for_timeout(1200)
            if self.ctx.pages and len(self.ctx.pages) > 1:
                print('可能打开了新标签；用「切换标签」选择它。')
            print('已执行原生点击；实际导航结果请查看浏览器。')
        except PlaywrightError as e:
            print(f'点击失败: {str(e)[:250]}')

    def _video_candidates(self):
        result = []
        for f in frames_of(self.page):
            try:
                vids = f.evaluate(VIDEO_JS)
                for v in vids:
                    if v['visible']:
                        result.append((f, v['index'], v))
            except PlaywrightError:
                continue
        return result

    def watch(self):
        candidates = self._video_candidates()
        if not candidates:
            print('没有检测到可见 HTML5 视频。可能是视频页尚未打开、播放器懒加载或专用播放器。')
            print('请在浏览器点开对应视频，手动点播放，再重试。')
            return
        for i, (f, index, v) in enumerate(candidates):
            print(f'  [{i}] {safe_url(f.url)[:85]} | video={index} paused={v["paused"]}')
        pick = input('监测哪个视频（默认 0）: ').strip() or '0'
        if not pick.isdigit() or int(pick) >= len(candidates):
            print('序号无效')
            return
        f, index, _ = candidates[int(pick)]
        vid = f.locator('video').nth(index)
        try:
            before = f.evaluate(VIDEO_JS)[index]
            if before['paused']:
                # Native UI only; never manipulate currentTime, rate, media events or completion API.
                pressed = False
                for sel in ('.vjs-big-play-button', '.prism-big-play-btn', '.play-button', 'button[aria-label*="播放"]'):
                    try:
                        button = f.locator(sel).first
                        if button.count() and button.is_visible():
                            button.click(timeout=1800)
                            pressed = True
                            break
                    except PlaywrightError:
                        pass
                if not pressed:
                    try:
                        vid.click(timeout=1500)
                    except PlaywrightError:
                        pass
            time.sleep(2)
            current = f.evaluate(VIDEO_JS)[index]
            if current['paused']:
                print('播放器仍处于暂停状态。请在浏览器手动点播放后重新执行。')
                return
            print('开始监测真实播放时间。Ctrl+C 停止。不会跳过、伪造进度或自动过验证弹窗。')
            last = current['current']
            still = 0
            while True:
                current = f.evaluate(VIDEO_JS)[index]
                dur = current['duration']
                print(f'\r实际视频进度: {current["current"]:.1f}s / {f"{dur:.1f}s" if dur else "未知"}; 播放中={not current["paused"]}', end='', flush=True)
                if current['ended']:
                    print('\n本地播放器报告已播放结束；请在超星页面核对任务点是否完成。')
                    self.record_video(f.url, index, current, 'ended_local')
                    break
                if abs(current['current'] - last) < .3:
                    still += 1
                else:
                    still = 0
                if current['paused'] and still >= 3:
                    print('\n播放器暂停。可能遇到课程要求的交互，请手动处理。')
                    self.record_video(f.url, index, current, 'paused')
                    break
                if still >= 15:
                    print('\n超过 30 秒没有进度变化，停止监测以免误报。')
                    self.record_video(f.url, index, current, 'stalled')
                    break
                last = current['current']
                time.sleep(2)
        except KeyboardInterrupt:
            try:
                self.record_video(f.url, index, current, 'interrupted')
            except Exception:
                pass
            print('\n已停止监测，保存最近一次读取到的播放进度。')
        except (PlaywrightError, IndexError) as e:
            print(f'\n播放器刷新或离开页面: {str(e)[:160]}')

    def record_video(self, frame_url, index, status, result):
        path = OUTPUT / 'video_progress.jsonl'
        path.parent.mkdir(exist_ok=True)
        with path.open('a', encoding='utf-8') as out:
            out.write(json.dumps({'time': stamp(), 'frame': safe_url(frame_url), 'video_index': index,
                                  'current_seconds': status['current'], 'duration_seconds': status['duration'],
                                  'result': result, 'platform_confirmed': False}, ensure_ascii=False)+'\n')

    def extract_questions(self):
        self.questions = []
        for fi, f in enumerate(frames_of(self.page)):
            try:
                blocks = f.evaluate(EXTRACTOR)
                for data in blocks:
                    self.questions.append(LocatedQuestion(self.page, f, data, 0, fi))
            except PlaywrightError:
                pass
        if not self.questions:
            print('未找到题目。请在浏览器进入章节测验页面、滚动到题目加载出来后重试。')
            return
        export = []
        for i, q in enumerate(self.questions):
            d = q.data
            export.append({'number': i+1, 'frame_url': safe_url(q.frame.url), **d})
            opts = ' | '.join(f'{x["key"]}: {x["text"][:60]}' for x in d['options'])
            print(f'\n[{i+1}] {d["type"]}: {d["question"][:140]}\n    {opts}')
            if d['has_visual']:
                print('    [包含图片/公式，建议开启视觉识别并核对]')
        save_json(OUTPUT / 'questions.json', {'created': stamp(), 'page': safe_url(self.page.url), 'questions': export})
        print(f'\n已导出 {len(self.questions)} 道题到 output/questions.json。')

    def ask_ai(self):
        if not self.questions:
            print('请先「提取题目」。')
            return
        if not os.getenv('OPENAI_API_KEY'):
            print('没有找到 OPENAI_API_KEY。题目已导出，可手动发给 ChatGPT，或配置密钥后重试。')
            return
        try:
            from openai import OpenAI
        except ImportError:
            print('缺少 openai 包：pip install -r requirements.txt')
            return
        client = OpenAI()
        model = os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')
        try:
            notes = (ROOT / 'course_notes.txt').read_text(encoding='utf-8')[:20000]
        except FileNotFoundError:
            notes = ''
        print(f'使用 {model}，将逐题请求模型。图片题可发送题目截图（可能包含个人课程信息）。')
        if input('确认将题目发送给 AI API？输入 YES 继续: ').strip() != 'YES':
            return
        for i, q in enumerate(self.questions):
            d = q.data
            task = {
                'type': d['type'], 'question': d['question'], 'full_text': d['full_text'],
                'options': d['options'], 'course_notes': notes,
                'required_json': {'answer': ['选项key，或填空文本'], 'explanation': '原因', 'confidence': .5}
            }
            content: list[dict] = [{'type': 'input_text', 'text': json.dumps(task, ensure_ascii=False)}]
            if d['has_visual']:
                try:
                    image = q.frame.locator(f'[data-study-helper-q="{d["id"]}"]').screenshot(timeout=5000)
                    content.append({'type': 'input_image', 'image_url': 'data:image/png;base64,' + base64.b64encode(image).decode('ascii')})
                except PlaywrightError:
                    print(f'  [{i+1}] 截图不可用，将使用纯文字推断。')
            try:
                rsp = client.responses.create(
                    model=model,
                    instructions=('你是课程练习辅导助手。仅基于提供的题干、可见选项和资料提出解答。'
                                  '不得臆造缺失的图表、公式或题干。不能确定时 confidence 必须低。'
                                  '严格输出一个 JSON 对象，只有 answer 数组、explanation 字符串、confidence 数字。'
                                  '单选和判断只输出一个选项 key，多选输出全部 key；填空输出一项完整文本。'),
                    input=[{'role': 'user', 'content': content}],
                )
                q.answer = validate_suggestion(d, clean_json(rsp.output_text))
                print(f'  [{i+1}/{len(self.questions)}] {q.answer["answer"]} 可信度(自评)={q.answer["confidence"]:.2f}  {q.answer["explanation"][:110]}')
            except Exception as e:
                print(f'  [{i+1}/{len(self.questions)}] AI 失败/结果不匹配: {str(e)[:200]}')
                q.answer = None
            self.export_answers()
        print('已写入 output/answer_suggestions.json；请核对后再填写。')

    def ollama_suggest(self, confirm: bool = True):
        """Get model suggestions from locally hosted Ollama; no account/API key required."""
        if not self.questions:
            print('请先提取题目（菜单 4）。')
            return
        from local_ai import ask_ollama
        if confirm:
            print(f'本地模型：{os.getenv("OLLAMA_MODEL", "qwen3-vl:4b")}；访问 127.0.0.1:11434。')
            if input('确认本地 AI 分析这些题目？输入 YES: ').strip() != 'YES':
                return
        notes_path = ROOT / 'course_notes.txt'
        notes = notes_path.read_text(encoding='utf-8')[:14000] if notes_path.exists() else ''
        for i, q in enumerate(self.questions, 1):
            image = None
            if q.data.get('has_visual'):
                try:
                    image = q.frame.locator(f'[data-study-helper-q="{q.data["id"]}"]').screenshot(timeout=5000)
                except PlaywrightError:
                    print(f'  [{i}] 含图片但无法截图，跳过，避免猜测。')
                    continue
            try:
                q.answer = validate_suggestion(q.data, ask_ollama(q.data, image=image, notes=notes))
                print(f'  [{i}/{len(self.questions)}] {q.answer["answer"]}；{q.answer["explanation"][:90]}')
            except Exception as e:
                print(f'  [{i}/{len(self.questions)}] 本地 AI 失败：{str(e)[:170]}')
                q.answer = None
                if 'Ollama 无法连接' in str(e) or 'model' in str(e).lower() or '只允许本机' in str(e):
                    break
            self.export_answers()
        self.export_answers()
        print('本地解答建议已保存到 output/answer_suggestions.json。')

    def open_chatgpt_handoff(self):
        """Open consumer ChatGPT only; no automated scraping/reading/submitting to chat UI."""
        if not self.questions:
            print('请先提取题目。')
            return
        chosen = [
            {'number': i, 'type': q.data['type'], 'question': q.data['question'],
             'options': q.data['options'], 'full_text': q.data['full_text'][:1800]}
            for i, q in enumerate(self.questions, 1)
        ]
        prompt = (
            '请帮助我学习并核对下面的章节练习。逐题输出答案和一句理由；看不到的图片/公式请不要猜。\n'
            '最后请严格输出 JSON：{\"answers\":[{\"number\":1,\"answer\":[\"A\"],'
            '\"explanation\":\"理由\",\"confidence\":0.6}]}。'
            '题目 number 必须与输入一致；若不确定，不要给出答案，用空数组。\n\n'
            + json.dumps(chosen, ensure_ascii=False, indent=2)
        )
        target = OUTPUT / 'chatgpt_prompt.txt'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(prompt, encoding='utf-8')
        try:
            import tkinter as tk
            window = tk.Tk()
            window.withdraw()
            window.clipboard_clear()
            window.clipboard_append(prompt)
            window.update()
            window.destroy()
            print('题目已复制到剪贴板。')
        except Exception:
            print('无法写入系统剪贴板，请直接打开 output/chatgpt_prompt.txt 复制。')
        try:
            tab = self.ctx.new_page()
            tab.goto('https://chatgpt.com/', wait_until='domcontentloaded', timeout=20000)
        except PlaywrightError as exc:
            print(f'ChatGPT 页面未能打开：{str(exc)[:110]}；请手动打开 https://chatgpt.com/')
        print('已生成 output/chatgpt_prompt.txt；在 ChatGPT 网页粘贴并发送。')
        print('把返回的 JSON 保存为 output/chatgpt_answers.json，然后在菜单选「导入手动获取的答案」。')
        print('不会通过程序自动读取 ChatGPT 网页回复。')

    def import_manual_answers(self):
        path = OUTPUT / 'chatgpt_answers.json'
        if not path.exists():
            print(f'尚未找到 {path}；请将网页版 ChatGPT 回复的 JSON 保存到这个文件。')
            return
        if not self.questions:
            print('先回到测验页面，菜单 4 重新提取相同顺序的题目。')
            return
        try:
            content = clean_json(path.read_text(encoding='utf-8'))
            data = content.get('answers', [])
            if not isinstance(data, list):
                raise ValueError('answers 应是数组')
            imported = 0
            for item in data:
                n = item.get('number')
                if not isinstance(n, int) or not (1 <= n <= len(self.questions)):
                    continue
                q = self.questions[n-1]
                try:
                    q.answer = validate_suggestion(q.data, item)
                    imported += 1
                except ValueError as exc:
                    print(f'  第 {n} 题不匹配：{exc}')
            self.export_answers()
            print(f'已导入 {imported} 道题的建议。请核对再执行菜单 6 填写。')
        except (OSError, ValueError, TypeError) as exc:
            print(f'读取失败：{exc}')

    def batch_courses(self):
        from batch_runner import run_batch
        run_batch(self)

    def survey_courses(self):
        from batch_runner import survey_target_courses
        survey_target_courses(self, interactive=True)

    def export_answers(self):
        save_json(OUTPUT / 'answer_suggestions.json', {
            'created': stamp(), 'page': safe_url(self.page.url),
            'questions': [{'number': i+1, 'id': q.data['id'], 'type': q.data['type'],
                           'question': q.data['question'], 'options': q.data['options'],
                           'suggestion': q.answer} for i, q in enumerate(self.questions)]
        })

    def review_and_fill(self):
        ready = [q for q in self.questions if q.answer and q.data['type'] in ('single', 'multiple', 'true_false')]
        if not ready:
            print('没有可填写的客观题建议。填空题目前只提供答案建议，不自动操作富文本编辑器。')
            return
        print('\n请逐题核对建议（也可以在 output/answer_suggestions.json 阅读详细解释）：')
        for i, q in enumerate(self.questions):
            if q.answer:
                print(f' [{i+1}] {q.data["question"][:90]} => {", ".join(q.answer["answer"])} '
                      f'(自评 {q.answer["confidence"]:.2f})')
        print('注意：AI 自评可信度并非实测正确率；页面结构可能改变。')
        picked = input('输入要填写的题号，如 1,3,5 或 all；直接回车取消: ').strip().lower()
        if not picked:
            return
        if picked == 'all':
            chosen = set(range(1, len(self.questions)+1))
        else:
            try:
                chosen = {int(x.strip()) for x in picked.split(',')}
            except ValueError:
                print('格式错误')
                return
        if input('确认已人工核对上述题目且同意在浏览器填写？输入 FILL: ').strip() != 'FILL':
            return
        for i, q in enumerate(self.questions, 1):
            if i not in chosen or q not in ready:
                continue
            try:
                success, reason = self.fill_choice(q)
                print(f'  [{i}] {"已尝试填写" if success else "失败"}: {reason}')
            except PlaywrightError as e:
                print(f'  [{i}] 页面变化，填写失败: {str(e)[:140]}')
        print('完成填写尝试。请在浏览器复核每个勾选状态，最后使用平台原生提交按钮。')

    @staticmethod
    def fill_choice(q: LocatedQuestion) -> tuple[bool, str]:
        # Playwright native user-like clicks; no direct modification of app's answer arrays/APIs.
        root = q.frame.locator(f'[data-study-helper-q="{q.data["id"]}"]').first
        if not root.count():
            return False, '题目节点已刷新，请重新提取题目'
        for key in q.answer['answer']:
            opt = root.locator(f'[data-study-helper-option="{key}"]').first
            if not opt.count():
                return False, f'未找到选项 {key}'
            inp = opt.locator('input[type="radio"],input[type="checkbox"]').first
            if inp.count() and inp.is_checked():
                continue
            if inp.count() and inp.is_visible() and inp.is_enabled():
                inp.check(timeout=4000)
            else:
                # Prefer native label click for visually-hidden form inputs.
                label = opt.locator('label').first
                if label.count() and label.is_visible():
                    label.click(timeout=4000)
                else:
                    opt.click(timeout=4000)
            time.sleep(.6)
            if inp.count() and not inp.is_checked():
                return False, f'{key} 点击后未能验证选中，请人工检查'
        if q.data['type'] == 'multiple':
            # Existing choices not in the AI suggestion may stay selected; report this instead of claiming success.
            selected = []
            for item in q.data['options']:
                n = root.locator(f'[data-study-helper-option="{item["key"]}"] input[type="checkbox"]').first
                if n.count() and n.is_checked():
                    selected.append(item['key'])
            if selected and set(selected) != set(q.answer['answer']):
                return False, f'当前已选 {selected}，与建议不一致；请手动清理多余选项'
        return True, ','.join(q.answer['answer'])

    def diagnose_navigation(self):
        from navigation_debug import capture_navigation
        capture_navigation(self)

    def diagnostic(self):
        """Read-only, minimally revealing cross-tab selector report; no Cookie or raw DOM dump."""
        from batch_runner import SCAN_JS
        summary = {'time': stamp(), 'selected_tab': None, 'tabs': []}
        for i, page in enumerate(self.ctx.pages):
            if page.is_closed():
                continue
            if page is self.page:
                summary['selected_tab'] = i
            tab = {'number': i, 'page': safe_url(page.url), 'title': page.title()[:120], 'frames': []}
            for f in frames_of(page):
                try:
                    selector_data = f.evaluate(r'''() => {
                      const sel = ['.chapter_unit','.chapter_item','.catalog_name','.clicktitle',
                        '.posCatalog_name','.ncells','[data-chapter-id]', 'video', 'iframe',
                        '.TiMu', '.questionLi', '.mark_item'];
                      const counts = Object.fromEntries(sel.map(s => [s, document.querySelectorAll(s).length]));
                      const maybeLessons = [...document.querySelectorAll('a,span,div')]
                        .filter(el => el.children.length === 0 && /^\s*\d{1,2}\.\d+/.test((el.textContent || '').trim()))
                        .slice(0, 12)
                        .map(el => ({tag:el.tagName.toLowerCase(), cls:String(el.className).slice(0,100),
                          parentTag:el.parentElement?.tagName.toLowerCase() || '',
                          parentCls:String(el.parentElement?.className || '').slice(0,100)}));
                      return {counts, possible_lesson_nodes: maybeLessons};
                    }''')
                    chapters = f.evaluate(SCAN_JS)
                    tab['frames'].append({'url': safe_url(f.url), 'selector_counts': selector_data['counts'],
                                          'candidate_count': len(chapters),
                                          'possible_lesson_nodes': selector_data['possible_lesson_nodes']})
                except PlaywrightError:
                    tab['frames'].append({'url': safe_url(f.url), 'error': 'unreadable'})
            summary['tabs'].append(tab)
        save_json(OUTPUT / 'diagnostic.json', summary)
        print(f'已保存 output/diagnostic.json，包含 {len(summary["tabs"])} 个标签页的结构统计。')
        print('不会记录 Cookie、URL 参数或题目原文；分享前仍请检查页面标题和 URL 路径。')


def menu(helper: Helper):
    from course_tasks import format_summary, saved_states
    actions = {
        '1': ('查看页面和 iframe / 视频状态', helper.info),
        '2': ('浏览课程/章节入口并点击', helper.discover_links),
        '3': ('监测当前视频的真实播放', helper.watch),
        '4': ('提取当前测验题目', helper.extract_questions),
        '5': ('AI 给出题目建议', helper.ask_ai),
        '6': ('核对并填写客观题', helper.review_and_fill),
        '7': ('切换浏览器标签', helper.select_page),
        '8': ('导出页面结构诊断', helper.diagnostic),
        'D': ('监控点击课程后 about:blank / 页面报错（35秒）', helper.diagnose_navigation),
        '9': ('Ollama 本地免费 AI 解题', helper.ollama_suggest),
        'A': ('自动遍历当前课程章节 / 顺序真实播放 / 题目批处理', helper.batch_courses),
        'E': ('扫描 courses.json 中的课程，优先处理未完成章节', helper.survey_courses),
        'B': ('打开 ChatGPT 网页并复制题目提示词', helper.open_chatgpt_handoff),
        'C': ('导入从 ChatGPT 网页手动复制的答案 JSON', helper.import_manual_answers),
        '0': ('退出', None)
    }
    saved = saved_states(OUTPUT)
    if saved:
        print('\n已保存的课程进度（不是平台成绩）：')
        for state in saved:
            print('  ' + format_summary(state))
    while True:
        print('\n===== 超星学习助手 v3.3 =====')
        for n, (name, _) in actions.items():
            print(f' {n}. {name}')
        key = input('请选择: ').strip()
        if key == '0':
            break
        if key not in actions:
            print('无效选项')
            continue
        try:
            actions[key][1]()
        except KeyboardInterrupt:
            print('\n已取消该操作。')
        except Exception as e:
            print(f'操作失败：{type(e).__name__}: {str(e)[:300]}')


def main():
    scan_only = '--scan-targets' in sys.argv
    run_pending = '--run-pending' in sys.argv
    OUTPUT.mkdir(exist_ok=True)
    with sync_playwright() as p:
        try:
            kwargs = {'headless': False, 'viewport': {'width': 1280, 'height': 900},
                      'accept_downloads': True}
            # Optional use of an officially installed browser: chrome or msedge.
            # Keep browser profiles separate, as their formats may differ.
            channel = os.getenv('CHAOXING_BROWSER', 'chromium').strip().lower()
            if channel not in ('chromium', 'chrome', 'msedge'):
                print('CHAOXING_BROWSER 只能为 chromium / chrome / msedge')
                sys.exit(1)
            if channel != 'chromium':
                kwargs['channel'] = channel
            browser_profile = ROOT / ('.browser_profile' if channel=='chromium' else f'.browser_profile_{channel}')
            print(f'浏览器内核：{channel}；独立登录目录：{browser_profile.name}')
            try:
                ctx = p.chromium.launch_persistent_context(str(browser_profile), **kwargs)
            except PlaywrightError:
                # Optional fallback for Linux workstations with system Chromium.
                browser = (shutil.which('chromium') or shutil.which('google-chrome')) if sys.platform != 'win32' else None
                if not browser:
                    raise
                ctx = p.chromium.launch_persistent_context(str(browser_profile), executable_path=browser, **kwargs)
        except PlaywrightError as e:
            print(f'浏览器启动失败：{e}\n请运行 python -m playwright install chromium')
            sys.exit(1)
        try:
            helper = Helper(ctx)
            if helper.page.url == 'about:blank':
                try:
                    helper.page.goto(START_URL, wait_until='domcontentloaded', timeout=30000)
                except PlaywrightError:
                    print(f'无法自动打开 {START_URL}；可直接在浏览器地址栏导航。')
            print('\n浏览器已启动。首次使用请在浏览器扫码/手动登录学习通，然后打开目标课程。')
            print(f'脚本不获取密码或 Cookie；登录态只保存在本机 {browser_profile.name} 目录。')
            if run_pending:
                from batch_runner import run_pending_courses
                run_pending_courses(helper)
            elif scan_only:
                print('正在只读扫描 courses.json 中的课程。不自动播放、不填写、不提交。')
                try:
                    helper.page.goto('https://i.chaoxing.com/base', wait_until='domcontentloaded', timeout=30000)
                except PlaywrightError:
                    print('未能打开课程主页，改为查看当前已打开的页面。')
                from batch_runner import survey_target_courses
                survey_target_courses(helper, interactive=False)
            else:
                input('准备好后回到终端按 Enter 打开菜单……')
                menu(helper)
        finally:
            ctx.close()


if __name__ == '__main__':
    main()
