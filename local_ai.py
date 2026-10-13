"""Local Ollama question suggestions; never accesses ChatGPT's consumer web UI."""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SYSTEM = (
    '你是网课练习学习助手。读取题干、选项、可能的课件笔记，给出建议答案及简短解释。'
    '对于看不清/缺失的图或题干，必须将 confidence 设置为 0，不得编造。'
    '严格输出 JSON 对象：{"answer":["A"],"explanation":"依据","confidence":0.5}。'
    '单选/判断填一项、多选填全部、填空填写完整文本。选项使用题目实际 key。'
)


def ask_ollama(question: dict, *, image: bytes | None = None, notes: str = '',
               model: str | None = None, endpoint: str | None = None,
               timeout: int = 180) -> dict:
    """Query the documented local REST API. Returns parsed JSON; caller validates keys."""
    model = model or os.environ.get('OLLAMA_MODEL', 'qwen3-vl:4b')
    endpoint = (endpoint or os.environ.get('OLLAMA_URL', 'http://127.0.0.1:11434')).rstrip('/')
    if not (endpoint.startswith('http://127.0.0.1:') or endpoint.startswith('http://localhost:')):
        # Remote Ollama can unintentionally expose question data and often has no auth.
        raise ValueError('只允许本机 Ollama；远程服务器请通过 SSH -L 11434:localhost:11434 建立本地端口转发。')
    detail = {'type': question.get('type'), 'question': question.get('question'),
              'options': question.get('options'), 'full_text': question.get('full_text'),
              'course_notes': notes[:14000]}
    user = {'role': 'user', 'content': json.dumps(detail, ensure_ascii=False)}
    if image:
        import base64
        user['images'] = [base64.b64encode(image).decode('ascii')]
    payload = {'model': model, 'messages': [{'role': 'system', 'content': SYSTEM}, user],
               'stream': False, 'format': 'json', 'options': {'temperature': 0}}
    request = Request(endpoint + '/api/chat', data=json.dumps(payload).encode('utf-8'),
                      headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urlopen(request, timeout=timeout) as response:
            obj = json.load(response)
    except HTTPError as exc:
        raw = exc.read().decode('utf-8', 'replace')[:300]
        raise RuntimeError(f'Ollama 请求失败 HTTP {exc.code}：{raw}') from exc
    except URLError as exc:
        raise RuntimeError('Ollama 无法连接。请先启动 Ollama，并运行 ollama pull qwen3-vl:4b') from exc
    content = obj.get('message', {}).get('content', '')
    if not content:
        raise ValueError('Ollama 返回空回答')
    from study_helper import clean_json
    return clean_json(content)
