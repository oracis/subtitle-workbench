# 字幕工作台

拖一个音频文件夹进去，出来就是能导进剪映的 SRT。单文件 tkinter GUI，不装任何第三方库。

核心算法（气口对齐、词边界断句、漏读补扫）与 CLI 版 [groq-srt](https://github.com/oracis/groq-srt) 同源，这里多了队列、热词、剪映草稿和打包好的 exe。

## 用

**exe**：`dist/字幕工作台.exe`，双击即用，不需要 Python。

**源码**：

```bash
python subtitle_workbench.py
```

首次启动会生成 `config.json`，也可以在界面里填。Groq API Key 在 [console.groq.com](https://console.groq.com) 免费申请。

## 界面

- **音频队列** —— 点「＋ 添加音频」，或者把文件 / 整个文件夹**直接拖进窗口**，支持批量顺序处理
- **Groq 设置** —— Key、模型（`whisper-large-v3` / turbo）、语种、代理
- **剪映草稿** —— 勾上就直接生成草稿并打开剪映，等于省掉导入那一步
- **热词** —— 每行一条 `错别字=正确词`，既注入 Whisper 的 prompt，也在出字幕时做替换
- **识别引导** —— 热词 + 跨切块上文续接，减少切块边界的错字
- **音频对齐** —— 用 VAD 测真实停顿来校正 Whisper 的时间漂移，关掉会快一些但气口会飘

## 背后做了什么

跟普通"调 Whisper 转字幕"的差别在识别之后：

1. **不按时间戳重排词序**。Whisper 的 words 是文本序输出的，词时间戳独立回归、相邻常倒挂 0.2 秒；排序会把「他此前创作的《大英博物馆世界简史》」打乱成「他此前创《作的大英 / 博物馆简世界》史」。这里只做保序 + 时间轴单调化。
2. **补扫块内跳读**。长音频里 Whisper 会成段漏读，用字/秒密度当探针（低于全片中位 55% 即可疑），把可疑段切出来重转写。
3. **断句跟气口**。`silencedetect` 取静音补集得到真实语音区间，区间首词标记为断点。
4. **切点不劈词**。jieba 词典裁出 70860 个词做双向最大匹配，切点只在词边界上选，避免「保｜命」「大英博｜物馆」。

实测 20 分钟讲书音频，与剪映原生导出对照：**719 条 / 均 9.6 字 / 2.13 秒 / 0 重叠 / 0 闪读**，字符一致率 96.7%（剩下的大半是数字写法差异）。

## 打包

```bash
py -3.15 -m PyInstaller 字幕工作台.spec --noconfirm --clean
```

`zh_words.txt`（分词词表，581KB）通过 spec 的 `datas` 打进 exe，运行时按 `sys._MEIPASS` 查找。要用带 tkinter 的解释器打包，WorkBuddy/部分精简版 Python 没有 tkinter。

## 配置

`config.json` 不入库（含 Key 和本机路径），模板见 `config.example.json`。`cfg_ver` 字段用于配置迁移，旧版本配置会自动升级到当前默认值（单条 11 字 / 4.5 秒）。

`from_srt_script` 是「SRT + 音频 → 剪映草稿」那一步用的外部脚本路径，不属于本仓库，需要自己指定；不填就只能导出 SRT，草稿要手动导入。

## 测试

```bash
py -3.15 test_wb_pipeline.py
py -3.15 test_dragdrop.py
```

## 依赖

Python 标准库 + ffmpeg。ffmpeg 会自动探测（含剪映自带的 `ffmpeg.exe`），也可以用界面上的代理/路径配置指定。
