# -*- coding: utf-8 -*-
"""subtitle_workbench 的 transcribe/_upload_one/split_audio 本地验证
（monkeypatch 上传层，不发真实请求）。与 groq-srt/test_pipeline.py 对应。"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import subtitle_workbench as swb

logs = []
LOG = lambda m: logs.append(m)
STOP = threading.Event()

# ---------- 单元：词表与 prompt 构建 ----------
v = swb.build_vocab_text(["心理治疗", "心理治疗,认知疗法", "欧文·亚隆"])
assert v == "心理治疗 认知疗法 欧文·亚隆", v
assert swb.build_vocab_text("甲，乙;丙 丁") == "甲 乙 丙 丁"
p = swb.build_prompt("心理治疗", "上文结尾")
assert p == "心理治疗 上文结尾", p
p2 = swb.build_prompt("词" * 400, "尾" * 100)
assert len(p2) <= swb.PROMPT_MAX_CHARS and p2.endswith("尾" * 100)
print("[OK] build_vocab_text / build_prompt")

# ---------- split_audio 重叠切块（真实 ffmpeg + 变频正弦） ----------
import subprocess
import tempfile
import array

FFMPEG = swb.find_ffmpeg()
NO_WINDOW = 0x08000000
tmp = tempfile.mkdtemp(prefix="wbtest-")
src = os.path.join(tmp, "src.m4a")
inputs = []
for i in range(12):
    inputs += ["-f", "lavfi", "-i", "sine=frequency=%d:duration=5" % (200 * (i + 1))]
fc = "".join("[%d]" % i for i in range(12)) + "concat=n=12:v=0:a=1[out]"
r = subprocess.run([FFMPEG, "-y", "-v", "error"] + inputs +
                   ["-filter_complex", fc, "-map", "[out]",
                    "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "64k", src],
                   capture_output=True, text=True, creationflags=NO_WINDOW)
assert r.returncode == 0, r.stderr[:500]
total = swb.m4a_end_time(src)
out_dir = os.path.join(tmp, "chunks")
os.makedirs(out_dir, exist_ok=True)
chunks = swb.split_audio(src, out_dir, 4, FFMPEG, overlap=3.0)
assert len(chunks) == 4, len(chunks)
T = total / 4
for i, (fp, start) in enumerate(chunks):
    assert abs(start - max(0.0, i * T - 3.0)) < 1e-6, (i, start)
print("[OK] split_audio 重叠切块 offset（0/9/24/39 节奏）: %s"
      % ["%.1f" % s for _, s in chunks])

# ---------- transcribe 全链路（monkeypatch 上传） ----------
VOCAB = ["心理治疗", "认知疗法"]
BLOCKS = [
    [{"start": 1.0, "end": 3.0, "text": "开场第一句。"},
     {"start": 13.0, "end": 14.5, "text": "交界句子完整版。"},
     {"start": 17.0, "end": 17.5, "text": "半句"}],
    [{"start": 0.5, "end": 2.5, "text": "交界句子完整版。"},
     {"start": 4.8, "end": 6.2, "text": "半句"},
     {"start": 7.0, "end": 9.0, "text": "块1独有句。"}],
    [{"start": 0.3, "end": 2.0, "text": "块2开头。"}],
]
calls = []


def fake_upload(audio_path, key, model, language, proxy, log, prompt=None):
    idx = len(calls)
    calls.append(prompt)
    return {"segments": [dict(s) for s in BLOCKS[idx]]}


swb._upload_one = fake_upload
fake_chunks = []
for i, off in enumerate([0.0, 12.0, 24.0]):
    fp = os.path.join(tmp, "seg%d.m4a" % i)
    with open(fp, "wb") as f:
        f.write(b"\x00" * 4096)
    fake_chunks.append((fp, off))
swb.split_audio = lambda *a, **k: fake_chunks

big = os.path.join(tmp, "big.m4a")
with open(big, "wb") as f:
    f.write(b"\x00" * (5 * 1024 * 1024))
result = swb.transcribe(big, "gsk_test", "whisper-large-v3", "zh", None,
                        LOG, FFMPEG, STOP, vocab=VOCAB, use_prompt=True)
segs = result["segments"]
texts = [s["text"] for s in segs]

assert calls[0] == "心理治疗 认知疗法", calls[0]
assert "开场第一句" in calls[1] and calls[1].startswith("心理治疗"), calls[1]
assert "块1独有句" in calls[2], calls[2]
print("[OK] prompt 链式传递（词表 + 上一块结尾）")

assert texts.count("交界句子完整版。") == 1, texts
trunc = [s for s in segs if s["text"] == "半句"]
assert len(trunc) == 1 and (trunc[0]["start"], trunc[0]["end"]) == (16.8, 18.2), trunc
assert "块1独有句。" in texts and "块2开头。" in texts
i = texts.index("块1独有句。")
assert (segs[i]["start"], segs[i]["end"]) == (19.0, 21.0), segs[i]
starts = [s["start"] for s in segs]
assert starts == sorted(starts)
print("[OK] 去重/完整版替换/offset/时序")

# ---------- multipart body 含 prompt ----------
captured = {}


def fake_request(method, path, key, proxy, body=None, headers=None, retries=3):
    captured["body"] = body
    return 200, b'{"segments": []}'


import importlib
importlib.reload(swb)
swb._request = fake_request
x = os.path.join(tmp, "x.m4a")
with open(x, "wb") as f:
    f.write(b"\x00" * 2048)
swb._upload_one(x, "k", "whisper-large-v3", "zh", None, LOG, prompt="测试引导词")
assert b'name="prompt"' in captured["body"]
assert "测试引导词".encode("utf-8") in captured["body"]
captured.clear()
swb._upload_one(x, "k", "whisper-large-v3", "zh", None, LOG, prompt=None)
assert b'name="prompt"' not in captured["body"]
print("[OK] multipart body 的 prompt 字段（有/无两种形态）")

# ---------- use_prompt=False 全禁用 ----------
calls.clear()
importlib.reload(swb)
swb._upload_one = fake_upload
swb.split_audio = lambda *a, **k: fake_chunks
swb.transcribe(big, "gsk_test", "whisper-large-v3", "zh", None,
               LOG, FFMPEG, STOP, vocab=VOCAB, use_prompt=False)
assert all(c is None for c in calls)
print("[OK] use_prompt=False 时完全不注入")

print("\nALL PASS")
sys.exit(0)
