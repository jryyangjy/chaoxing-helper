# 超星学习通网课助手（Windows）

基于 Playwright 的本地浏览器助手，适配超星学习通 / 泛雅（`mooc2-ans.chaoxing.com`）的新版章节目录。它会在你自己登录的浏览器里扫描课程目录、按顺序真实播放视频、处理视频中的弹题，并把章节测验导出给你或 AI 核对。

## 设计原则

- **只做正常播放**：通过播放器原生按钮播放并监测 `currentTime` 的真实增长，不修改播放进度、倍速或 `paused` 状态，不调用完成任务的接口，不伪造观看时长。
- **不绕过验证**：遇到验证码、人脸、滑块或「请人工确认」等弹层会停下，交给本人处理。
- **不自动交卷**：章节测验只导出题目、生成建议并在核对后填写，最终提交由本人点击。
- **不碰账号密码**：登录由你在浏览器里手动完成。登录状态只保存在本机的 `.browser_profile*` 目录，报告里的网址会去掉查询参数（其中可能带有令牌）。

使用前请自行确认学校和平台的规定。因使用本工具产生的一切后果由使用者自负。

## 安装

1. 安装 Python 3.10 或更高版本，安装时勾选 "Add Python to PATH"。
2. 下载本仓库，双击 `start_windows_chrome.bat`（平时用 Edge 的话，双击 `start_windows_edge.bat`）。脚本会安装依赖并启动程序。

命令行方式：

```powershell
python -m pip install -r requirements.txt
$env:CHAOXING_BROWSER = "chrome"   # 可选 chromium / chrome / msedge
python study_helper.py
```

如果选择 `chromium`，需要先执行 `python -m playwright install chromium`（`start_windows.bat` 会自动执行）。

## 配置要处理的课程

菜单 `E` 和命令行参数 `--scan-targets`、`--run-pending` 只处理 `courses.json` 里列出的课程。把示例文件复制一份再修改：

```powershell
Copy-Item courses.example.json courses.json
```

```json
[
  {"id": "history", "name": "中国近现代史纲要", "aliases": ["中国近现代史纲要（2026）"]}
]
```

- `id`：本地保存进度用的目录名，只用字母、数字、下划线。
- `name` / `aliases`：课程在学习通页面上显示的名称，用于匹配课程卡片和标题。

`courses.json` 已被 `.gitignore` 忽略，不会被提交。也可以用环境变量 `CHAOXING_COURSES_FILE` 指定其他路径。

## 使用

1. 启动后，在弹出的浏览器窗口里扫码或手动登录学习通，打开一门课程的「章节」页，再回到终端按 Enter。
2. 先选菜单 `8` 导出页面结构，确认章节数量。
3. 再选菜单 `A` 批量处理，首次建议起始和结束序号填同一节（例如 `2` 到 `2`），确认无误后再扩大范围。

| 菜单 | 功能 |
|---|---|
| 1 | 查看页面、iframe 和视频状态 |
| 2 | 浏览并点击课程/章节入口 |
| 3 | 监测当前视频的真实播放 |
| 4 | 提取当前测验题目 |
| 5 | 用 OpenAI API 给出题目建议 |
| 6 | 人工核对后填写客观题 |
| 7 | 切换浏览器标签页 |
| 8 | 导出页面结构诊断 |
| 9 | 用本地 Ollama 模型给出题目建议 |
| A | 遍历当前课程章节：顺序真实播放视频、导出题目 |
| B / C | 整理题目并打开 ChatGPT 网页 / 导入手动复制回来的答案 JSON |
| D | 诊断点击课程后停在 `about:blank` 或页面报错的情况 |
| E | 扫描 `courses.json` 中的课程，优先处理未完成章节 |

命令行参数：

- `python study_helper.py --scan-targets`：只读扫描配置的课程，不播放、不填写、不提交。
- `python study_helper.py --run-pending`：依次处理配置课程中目录标为「待完成」的章节。可以用环境变量 `CHAOXING_ONLY_COURSES=history,ai_intro` 只处理部分课程。

### 视频弹题

播放中出现弹题时，程序会把题目写入 `output/popup_pending.json`，并把截图保存为 `output/popup_question.png`，然后最多等待 180 秒，读取 `output/popup_reply.json` 中的答案：

```json
{"answer": ["B"], "explanation": "理由", "confidence": 0.9}
```

答案可以由你手动写入，也可以由其他脚本或 AI 写入。程序校验选项合法后点击对应选项，再点弹层自带的提交按钮。弹层出现「继续学习」时点击它，继续播放。超时没有答案就不提交，弹题保持打开。

批处理遇到章节测验时也用同样的方式逐题等待答案，填好后只点「暂时保存」，不点「提交」或「交卷」。

### AI 解题建议

- **本地免费**：安装 [Ollama](https://ollama.com/) 并执行 `ollama pull qwen3-vl:4b`，然后用菜单 `9`。可用环境变量 `OLLAMA_MODEL`、`OLLAMA_URL` 修改模型和地址。
- **OpenAI API**：执行 `python -m pip install openai` 并设置 `OPENAI_API_KEY`（可选 `OPENAI_MODEL`），然后用菜单 `5`。参见 `example.env.txt`。

AI 建议只用于参考，填写前务必核对；提交仍由本人完成。

## 输出文件

运行结果写在 `output/` 目录（已被 git 忽略）：

- `output/batch_report.json`：批处理进度，约每 15 秒保存一次，中断后菜单 `A` 会提示从上次未完成的章节继续。
- `output/courses/<id>/`：每门配置课程的目录状态、批处理报告和事件日志。
- `output/questions*.json`：导出的测验题目。
- `output/last_failure.json`、`output/navigation_diagnostic.json`：故障诊断。

结果中的状态含义：

- `video_ended_verify_platform`：本地播放已经结束，但需要以平台显示为准。
- `video_needs_manual_attention`：播放被阻断，例如 `foreground_required` 表示窗口不在前台时课程暂停了。
- `questions_need_review`：导出了题目，需要本人核对。

**本地播放结束不等于平台已经记录完成**，请以学习通页面上的任务点状态为准。

## 已知限制

- 有的课程要求窗口保持在前台才计时。程序不会伪造焦点；窗口被切走导致暂停时，会记录 `foreground_required` 并等待。
- 一节课里有多个独立任务页、文档阅读或非标准播放器时，不保证全部覆盖。未知任务会保留为待处理，不会当作完成。
- 学习通页面结构可能随时变化，选择器需要随之更新。

## 测试

测试只使用本地模拟页面和一个很短的 WebM 视频，不访问超星服务器：

```powershell
python -m pip install -U pytest
python -m pytest -q
```

## 隐私

- 不要分享 `.browser_profile*` 目录：里面的 Cookie 可以让别人直接使用你的登录状态。
- `output/` 里的题目、截图和课程信息可能包含个人信息，分享前请检查。
- 不要把账号密码或 API Key 写进代码、Issue 或配置文件后提交。

## 参考项目

- [DanMo661/Xuexitong-mcp](https://github.com/DanMo661/Xuexitong-mcp)：新版章节树与任务点解析（只读查询）。
- [Samueli924/chaoxing](https://github.com/Samueli924/chaoxing)：新旧接口与任务流程。

## 许可证

[MIT](LICENSE)
