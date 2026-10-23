#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""subtitle_workbench.py - 有声书字幕工作台（单文件 GUI）

一条龙：选音频 -> Groq Whisper 转录（自动转码/切块/重试）-> 热词纠错
-> 输出 SRT -> 自动生成剪映草稿（调 jianying-subtitle-gui 的 from_srt.py，
无需 ASR、无需手动导入字幕；剪映里打开即是带音频轨+字幕轨的可编辑工程）。

零第三方依赖（纯标准库 + tkinter）。Windows 双击即可运行。
配置文件与脚本同目录: config.json（key/热词表/模型/草稿选项）。
"""

import glob
import json
import os
import queue
import re
import shutil
import socket
import ssl
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# Windows：GUI 进程启动控制台子系统程序（ffmpeg/python/tasklist）时，系统会
# 额外弹一个控制台窗口。统一给 subprocess 加 CREATE_NO_WINDOW 压掉。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _run(cmd, **kw):
    """subprocess.run + 默认不弹控制台窗口。"""
    kw["creationflags"] = kw.get("creationflags", 0) | _NO_WINDOW
    return subprocess.run(cmd, **kw)

API_HOST = "api.groq.com"
API_PORT = 443
API_PATH = "/openai/v1/audio/transcriptions"
MODELS_PATH = "/openai/v1/models"
SIZE_LIMIT = 25 * 1024 * 1024
CHUNK_THRESHOLD = 4 * 1024 * 1024
OVERLAP_SEC = 3.0                   # 切块重叠秒数：每块头部带 3s 上文，交界句子由两块分别转写后择优
PROMPT_MAX_CHARS = 220              # Whisper prompt 上限约 448 token，中文 1 字≈1-1.5 token，保守截断
PROMPT_TAIL_CHARS = 120             # 上一块结尾带入 prompt 的字符数
DEFAULT_PROXY = "socks5h://127.0.0.1:10808"
DEFAULT_FROM_SRT = r"D:/jianying-subtitle-gui/app/from_srt.py"

# ---- 漏读补扫（Whisper 块内跳读）----
# 实测《众神》20 分钟音频：块 3 少 191 词、块 4 少 117 词，合计约 300 字
# 整段没输出，表现为"某段语音 6 秒只识别出 1 个字"。靠断句/纠错都救不回来
# ——音频里有内容，文本里根本没字。用随速密度当探针：正常 4~5 字/秒，
# 低于全片中位数的 55% 就切出来重识别。
GAP_DENSITY_RATIO = 0.55            # 低于全片中位密度的比例即视为可疑
GAP_MIN_DUR = 2.5                   # 短于此的语音段不判（样本太少易误判）
GAP_PAD = 2.0                       # 重切音频前后各扩的秒数（给模型上下文）

# ---- 字幕分段参数（依据剪映官方"按语音停顿断句"选项 + 行业字幕规范） ----
# 剪映识别字幕官方提供「智能断句 / 按语音停顿断句」两种模式，后者专治
# 断句错位——即本工具的"气口断开"。行业规范（多家剪辑指南一致）：
# 每行 15-20 个汉字（横屏取上限）、每条 1-3 秒、话音停字幕即停。
# 中文安全阅读速度约 4-6 字/秒，故 20 字配 6 秒上限。
CUE_MAX_CHARS = 11                  # 单条字幕最大显示宽度（汉字计 1，英文计 0.55）
# 实测剪映原生《众神》：703 条，中位 10 字、最长 16 字，13-16 字占 21%。
# 离线扫描 10/11/12/13/14 五档（用落盘的 6452 个词重放，不调 API）：
#   10 -> 748 条/均 9.2 字   11 -> 691 条/均 9.9 字   12 -> 644 条/均 10.6 字
# 11 与原生 703 条/均 9.8 字/2.23s 逐项吻合，故取 11；断点落在标点上时
# 再放宽到 1.25 倍（≈13 字，原生最长 16 字）
CUE_MAX_DUR = 4.5                   # 单条最长秒数
# 实测剪映原生《众神》703 条：最长条 4.40s、>4.5s 的 0 条。原先 6.0s 会漏出 3 条
# 4.5~5.8s 的超长条（均字数不变，纯粹是那句被念得慢）。降到 4.5 后条数只 +1
CUE_MIN_DUR = 1.0                   # 单条最短秒数（太短会闪跳）
BREAK_GAP = 0.6                     # 词间静音达到该值视为气口，断开（剪映停顿断句同思路）
STRONG_MIN_CHARS = 6                # 句末标点后断开的最小累计宽度
WEAK_BREAK_RATIO = 0.55             # 逗号级断点：累计宽度达到上限该比例才断

# ---- 音频对齐参数（用真实语音区间校正 Whisper 漂移的时间戳） ----
# Whisper 的词级时间戳是"推断"的而非"测量"的：时间 token 由模型像普通 token
# 一样预测，segment 内的长停顿会把它整体拉偏（社区公认的 "drift after
# silences"）。stable-ts 为此提供 adjust_by_silence()：先用静音检测拿到音频
# 里真实的语音区间，再把词钉回去。这里用 ffmpeg silencedetect 实现，零依赖。
ALIGN_TO_SPEECH = True              # 用音频静音区校正词时间戳（关掉=完全信 Whisper）
SILENCE_MIN_DUR = 0.35              # 短于此值的停顿不算语音区间边界
SPEECH_MIN_DUR = 0.12               # 短于此值的"语音"碎片视为噪声丢弃
CUE_TAIL = 0.10                     # 字幕结束比末词多留的秒数（余韵，勿越入长停顿）
ALIGN_BREAK_GAP = 0.40              # 对齐态的断句气口门槛：区间间隔达到该值才断
MIN_CUE_WIDTH = 4
LOOKBACK_GAP = 0.15                 # 回看切点：宽度将超限时，回过头找 cur 内
                                    # 间隔 >= 此值且最宽的位置断，而不是就地硬切
SPAN_MAX = 2.0                      # 单个词跨度超过它、字数又很少 -> Whisper 时间
                                    # 戳错乱（实测「这」单字跨 6.96s）
SPAN_FIX = 0.35                     # 错乱词修正后的跨度（正常单字约 0.2~0.4s）                   # 单条最小显示宽度：低于此值的碎条并入上一条，避免闪读
# 口播水词：剪映的断句引擎会忽略"嗯/啊"这类填充词周围的短停顿，不让它单独成条、
# 也不在这里断句。同理：上一条以水词结尾时，跨越这个气口继续接下文。
_FILLERS = set("嗯啊呃哦唉诶哎噢呀嘛吧呢哈")
SOFT_GAP = 0.25                     # 软断点：短停顿 + 标点同时命中才切（剪映加权思路）

# PyInstaller onefile：__file__ 指向临时解压目录，配置要跟 exe 走
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _config_path():
    """exe 目录可写就用 exe 目录；不可写（如装在 Program Files）回退
    LOCALAPPDATA，保证配置永远存得下。"""
    if os.path.isdir(BASE_DIR) and os.access(BASE_DIR, os.W_OK):
        return os.path.join(BASE_DIR, "config.json")
    return os.path.join(os.environ.get("LOCALAPPDATA", BASE_DIR),
                        "SubtitleWorkbench", "config.json")


CONFIG_PATH = _config_path()
AUDIO_EXTS = (".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".wma",
              ".mp4", ".mkv", ".mov", ".avi", ".webm")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ==================== 配置 ====================

def load_config():
    cfg = {"key": "", "model": "whisper-large-v3", "language": "zh",
           "hotwords": "", "proxy": DEFAULT_PROXY,
           "gen_draft": True, "draft_fix": False,
           "prompt_boost": True, "overlap": OVERLAP_SEC,
           "align_to_speech": ALIGN_TO_SPEECH,
           "cue_max_chars": CUE_MAX_CHARS, "cue_max_dur": CUE_MAX_DUR,
           "from_srt_script": DEFAULT_FROM_SRT}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    # ---- 配置迁移 ----
    # 老配置文件里 cue_max_chars 存着当时的默认值（20/10/13），会盖掉新的默认，
    # 导致"改了代码但用户看到的还是旧值"。未打 cfg_ver 或版本落后的配置，
    # 值正好命中旧默认序列时按新默认走；用户手动设过的其它值不动。
    # v4：默认定为 11（离线扫描 10~14 五档，11 与剪映原生 703 条/
    # 均 9.8 字/2.23s 逐项吻合）。历次迁移写进去的旧默认 20/10/13
    # 一并升到新默认；用户手改过的其它值不动。
    if cfg.get("cfg_ver", 0) < 4:
        if cfg.get("cue_max_chars") in (20, 10, 13):
            cfg["cue_max_chars"] = CUE_MAX_CHARS
        cfg["cfg_ver"] = 4
    # v5：单条最长秒数 6.0 -> 4.5。实测原生 703 条最长 4.40s、>4.5s 的 0 条，
    # 旧默认 6.0 会漏出几条 4.5~5.8s 的超长条。同 v4 的路子：只改命中旧默认的值。
    if cfg.get("cfg_ver", 0) < 5:
        if cfg.get("cue_max_dur") == 6.0:
            cfg["cue_max_dur"] = CUE_MAX_DUR
        cfg["cfg_ver"] = 5
    return cfg


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print("保存配置失败: %s" % e, file=sys.stderr)
        return False


# ==================== ffmpeg / 音频处理 ====================

def find_ffmpeg(explicit=None):
    cands = []
    if explicit:
        cands.append(explicit)
    cands.append("ffmpeg")
    cands += glob.glob(r"D:\Jianying6.01\*\*\ffmpeg.exe")
    cands += glob.glob(r"D:\Jianying*\*\ffmpeg.exe")
    cands += glob.glob(r"C:\Program Files*\ffmpeg*\bin\ffmpeg.exe")
    for c in cands:
        try:
            _run([c, "-version"], capture_output=True, check=True)
            return c
        except Exception:
            continue
    raise RuntimeError("找不到可用的 ffmpeg。装一个，或在 config.json 里配置 ffmpeg 路径。")


def transcode(src, dst, ffmpeg, bitrate="64k"):
    cmd = [ffmpeg, "-y", "-v", "error", "-i", src,
           "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", bitrate, dst]
    r = _run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg 转码失败:\n" + (r.stderr or "")[:500])
    return os.path.getsize(dst)


def m4a_end_time(path):
    """解析 m4a/mp4 mvhd。注意：ffmpeg segment 切出的块，mvhd 值是
    「该块内容在原文件时间轴上的结束时刻」，不是块自身时长。"""
    try:
        with open(path, "rb") as f:
            fsize = os.path.getsize(path)

            def read_at(pos, ln):
                f.seek(pos)
                return f.read(ln)

            def walk(start, end):
                pos = start
                while pos + 8 <= end:
                    h = read_at(pos, 16)
                    if len(h) < 8:
                        return None
                    size = struct.unpack(">I", h[0:4])[0]
                    btype = h[4:8]
                    hs = 8
                    if size == 1:
                        if len(h) < 16:
                            return None
                        size = struct.unpack(">Q", h[8:16])[0]
                        hs = 16
                    elif size == 0:
                        size = end - pos
                    if size < hs or pos + size > end:
                        return None
                    if btype == b"moov":
                        r = walk(pos + hs, pos + size)
                        if r is not None:
                            return r
                    elif btype == b"mvhd":
                        v = read_at(pos + hs, 100)
                        ver = v[0]
                        if ver == 1:
                            ts = struct.unpack(">I", v[20:24])[0]
                            dur = struct.unpack(">Q", v[24:32])[0]
                        else:
                            ts = struct.unpack(">I", v[12:16])[0]
                            dur = struct.unpack(">I", v[16:20])[0]
                        return (dur / ts) if ts else None
                    pos += size
                return None

            return walk(0, fsize)
    except Exception:
        return None


def split_audio(src, out_dir, n_chunks, ffmpeg, overlap=OVERLAP_SEC):
    """把 src 切成 n_chunks 块，相邻块重叠 overlap 秒。

    块 i 覆盖 [i*T-overlap, (i+1)*T+overlap)（首/尾块按文件边界裁剪），
    每块头部自带 overlap 秒上文音频——交界句子由相邻两块分别转写，
    合并时按时间重叠择优（见 _merge_segments）。

    -ss 输入定位 + 流拷贝。实测（PCM 互相关，4 个采样点含非网格对齐位置）：
    实际解码起点与 -ss 请求值误差 0.0ms——ffmpeg 定位到前一个音频包后用
    elst 编辑列表精确修剪包内偏移。故 offset 即请求值，无需网格取整。

    返回 [(块路径, 该块起始 offset 秒), ...]。
    """
    total = m4a_end_time(src)
    if not total:
        raise RuntimeError("无法解析音频时长，切块失败。")
    seg_time = total / n_chunks
    ov = overlap if n_chunks > 1 else 0.0
    if ov > seg_time * 0.4:
        ov = seg_time * 0.4
    chunks = []
    for i in range(n_chunks):
        start = max(0.0, i * seg_time - ov)
        end = min(total, (i + 1) * seg_time + ov)
        if end - start < 1.0:
            continue  # 尾部残片（重叠已覆盖其内容）
        fp = os.path.join(out_dir, "seg%03d.m4a" % i)
        cmd = [ffmpeg, "-y", "-v", "error",
               "-ss", "%.3f" % start, "-i", src,
               "-c", "copy", "-t", "%.3f" % (end - start), fp]
        r = _run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("ffmpeg 切块失败:\n" + (r.stderr or "")[:500])
        if os.path.getsize(fp) < 10240:
            continue
        d = m4a_end_time(fp)
        if d and abs(d - (end - start)) > 0.5:
            print("[warn] 块 %d 时长异常: 期望 %.1fs 实得 %.1f"
                  % (i + 1, end - start, d))
        chunks.append((fp, start))
    if not chunks:
        raise RuntimeError("切块后没有有效文件。")
    return chunks


def srt_ts(sec):
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


# ==================== 代理隧道 + HTTP ====================

def make_client_context():
    return ssl.create_default_context()


def _socks5_handshake(sock, host, port):
    sock.sendall(b"\x05\x01\x00")
    resp = sock.recv(2)
    if len(resp) < 2 or resp[0] != 5:
        raise RuntimeError("SOCKS5 代理响应异常")
    if resp[1] == 0xFF:
        raise RuntimeError("SOCKS5 代理要求认证（本工具仅支持无认证的本地代理）")
    host_b = host.encode("ascii")
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b
                 + port.to_bytes(2, "big"))
    head = sock.recv(4)
    if len(head) < 4 or head[1] != 0:
        raise RuntimeError("SOCKS5 CONNECT 被拒 (code=%s)"
                           % (head[1] if len(head) > 1 else "?"))
    atyp = head[3] if len(head) > 3 else 0
    if atyp == 1:
        sock.recv(4)
    elif atyp == 3:
        ln = sock.recv(1)
        if ln:
            sock.recv(ln[0])
    elif atyp == 4:
        sock.recv(16)
    sock.recv(2)


def _http_connect(sock, host, port):
    req = ("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n"
           "Proxy-Connection: keep-alive\r\n\r\n" % (host, port, host, port))
    sock.sendall(req.encode("ascii"))
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    status = buf.split(b"\r\n", 1)[0]
    if b" 200" not in status:
        raise RuntimeError("HTTP 代理 CONNECT 失败: %s"
                           % status.decode("latin1", "replace")[:80])


def make_tls_tunnel(proxy):
    if not proxy or proxy.lower() in ("none", "direct", "off"):
        return None
    m = re.match(r"^(socks5h|socks5|socks4|http)://([^/:]+):(\d+)/?$",
                 proxy.strip(), re.I)
    if not m:
        raise RuntimeError("无法解析代理地址: %s" % proxy)
    scheme = m.group(1).lower()
    phost, pport = m.group(2), int(m.group(3))

    def make(host, port):
        s = socket.create_connection((phost, pport), timeout=30)
        s.settimeout(600)
        try:
            if scheme.startswith("socks"):
                _socks5_handshake(s, host, port)
            else:
                _http_connect(s, host, port)
            return make_client_context().wrap_socket(s, server_hostname=host)
        except Exception:
            try:
                s.close()
            except OSError:
                pass
            raise
    return make


class NetError(Exception):
    pass


def _friendly_net_error(e, proxy):
    msg = str(e)
    low = msg.lower()
    if "10061" in msg or "refused" in low:
        return "连不上代理 %s（连接被拒）。启动 Shadowsocks/代理软件。" % proxy
    if any(k in msg for k in ("10053", "10054", "10057")) \
            or "aborted" in low or "reset" in low:
        return "连接被中止（%s）。会自动重试，持续失败请换代理节点。" % msg[:70]
    return msg[:150]


def _read_chunked(sock, initial):
    buf = bytearray(initial)
    body = bytearray()
    while True:
        while b"\r\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise NetError("响应被截断（chunked 流的 chunk 头都没收全）")
            buf += chunk
        line, _, rest = bytes(buf).partition(b"\r\n")
        buf = bytearray(rest)
        try:
            size = int(line.split(b";")[0].strip(), 16)
        except ValueError:
            raise NetError("chunked 响应格式异常: %r" % line[:40])
        if size == 0:
            break
        while len(buf) < size + 2:
            chunk = sock.recv(65536)
            if not chunk:
                raise NetError("响应被截断（chunk 数据不完整）")
            buf += chunk
        body += buf[:size]
        del buf[:size + 2]
    return bytes(body)


def _read_body(sock, hdrs, initial):
    if b"content-length" in hdrs:
        clen = int(hdrs[b"content-length"])
        body = initial
        while len(body) < clen:
            chunk = sock.recv(65536)
            if not chunk:
                break
            body += chunk
        if len(body) < clen:
            raise NetError("响应体被截断（Content-Length=%d，实收 %d）" % (clen, len(body)))
        return body[:clen]
    if hdrs.get(b"transfer-encoding", b"").lower().find(b"chunked") >= 0:
        return _read_chunked(sock, initial)
    body = initial
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        body += chunk
    return body


def _read_response(sock):
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    parts = lines[0].split(b" ", 2)
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/"):
        raise RuntimeError("响应格式异常: %s" % head[:80].decode("latin1", "replace"))
    hdrs = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(b":")
        hdrs[k.strip().lower()] = v.strip()
    return int(parts[1]), _read_body(sock, hdrs, rest)


def _send_body_async(sock, body):
    def worker():
        try:
            view = memoryview(body)
            sent = 0
            while sent < len(view):
                n = min(65536, len(view) - sent)
                sock.sendall(view[sent:sent + n])
                sent += n
        except Exception:
            pass
    threading.Thread(target=worker, daemon=True).start()


def _request_once(method, path, key, proxy, body=None, headers=None):
    req_lines = ["%s %s HTTP/1.1" % (method, path),
                 "Host: %s:%d" % (API_HOST, API_PORT),
                 "Accept: application/json",
                 "Authorization: Bearer " + key,
                 "Connection: close"]
    for k, v in (headers or {}).items():
        req_lines.append("%s: %s" % (k, v))
    head = ("\r\n".join(req_lines) + "\r\n\r\n").encode("latin1")
    try:
        make = make_tls_tunnel(proxy) if proxy else None
        if make is not None:
            sock = make(API_HOST, API_PORT)
        else:
            raw = socket.create_connection((API_HOST, API_PORT), timeout=600)
            sock = make_client_context().wrap_socket(raw, server_hostname=API_HOST)
    except Exception as e:
        raise NetError(_friendly_net_error(e, proxy))
    try:
        try:
            sock.sendall(head)
            if body:
                _send_body_async(sock, body)
            return _read_response(sock)
        except Exception as e:
            raise NetError(_friendly_net_error(e, proxy))
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _request(method, path, key, proxy, body=None, headers=None, retries=3,
             on_retry=None):
    last = None
    for attempt in range(retries):
        try:
            return _request_once(method, path, key, proxy, body, headers)
        except NetError as e:
            last = e
            if attempt < retries - 1:
                if on_retry:
                    on_retry(attempt + 1, str(e))
                time.sleep(1.5 * (attempt + 1))
    raise last


def _die_for_status(status, detail):
    if status == 403:
        raise RuntimeError(
            "Groq 返回 403 - 出口 IP 被封锁。\n"
            "  自检: curl -x socks5h://127.0.0.1:10808 "
            "https://www.cloudflare.com/cdn-cgi/trace 看 loc= 是否非 CN；\n"
            "  若在 CN，换代理节点（美/日/新/欧）。")
    if status == 401:
        raise RuntimeError(
            "Groq 返回 401 - key 无效或已被撤销。\n"
            "  去 console.groq.com → API Keys 确认密钥还在，必要时重建（gsk_ 开头）。")
    if status == 429:
        raise RuntimeError(
            "Groq 返回 429 - 触发限流（免费层 20 请求/分钟、8 小时音频/天）。稍后再试。")
    raise RuntimeError("Groq API 错误 %d: %s" % (status, (detail or "")[:300]))


def preflight(key, proxy, log):
    log("[..] 预检 key 与出口区域（GET /models）...")
    try:
        status, raw = _request("GET", MODELS_PATH, key, proxy)
    except NetError as e:
        raise RuntimeError(str(e))
    if status != 200:
        _die_for_status(status, raw.decode("utf-8", "replace"))
    try:
        n = len(json.loads(raw.decode("utf-8")).get("data", []))
        log("[ok] key 有效，出口区域放行（可用模型 %d 个）" % n)
    except Exception:
        log("[ok] key 有效，出口区域放行")


# ==================== 识别引导（prompt）与重叠合并 ====================

def build_vocab_text(vocab):
    """vocab: 字符串列表或逗号/空格分隔的字符串 -> 去重后空格连接。"""
    if not vocab:
        return ""
    if isinstance(vocab, str):
        items = re.split(r"[,，;；\s]+", vocab)
    else:
        items = []
        for v in vocab:
            items += re.split(r"[,，;；\s]+", str(v))
    seen, out = set(), []
    for it in items:
        it = it.strip()
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return " ".join(out)


def build_prompt(vocab_text, prev_tail):
    """组 Whisper prompt：引导词 + 上一块结尾文本。

    prompt 对 Whisper 是"续写引导"：词表让解码器倾向正确写法，上文尾句
    恢复跨切块的上下文（交界处不断片）。上文放最后（紧邻解码起点）。
    总长受 448 token 上限约束，超长时优先保上文、砍词表。
    """
    vocab_text = (vocab_text or "").strip()
    prev_tail = (prev_tail or "").strip()
    tail = prev_tail[-PROMPT_TAIL_CHARS:] if prev_tail else ""
    p = (vocab_text + " " + tail).strip() if vocab_text else tail
    if len(p) > PROMPT_MAX_CHARS:
        room = PROMPT_MAX_CHARS - len(tail) - 1
        p = (vocab_text[:room] + " " + tail).strip() if room > 20 else tail
    return p


def _merge_segments(all_segs):
    """合并各块 segments（已加全局 offset），按时间重叠贪心去重。

    重叠切块会在交界处产出同一句子的两份转写：按时间重叠度合并——
    重叠超过阈值时保留时长更长的那份（通常就是未被截断的完整版），
    仅一块有的句子（另一块漏识）原样保留，顺带完成补缺。
    """
    segs = [s for s in all_segs if (s.get("text") or "").strip()]
    segs.sort(key=lambda s: (s.get("start", 0), s.get("end", 0)))
    kept = []
    for seg in segs:
        ss = seg.get("start", 0)
        se = seg.get("end", 0)
        dup_of = None
        for j in range(len(kept) - 1, max(-1, len(kept) - 4), -1):
            ps, pe = kept[j].get("start", 0), kept[j].get("end", 0)
            ov = min(pe, se) - max(ps, ss)
            shorter = min(pe - ps, se - ss)
            if ov > 0.45 and shorter > 0 and ov > shorter * 0.45:
                dup_of = j
                break
        if dup_of is None:
            kept.append(seg)
        elif (se - ss) > (kept[dup_of].get("end", 0)
                          - kept[dup_of].get("start", 0)) * 1.1:
            kept[dup_of] = seg  # 新版本明显更完整，替换
    return kept


# ---------- 词级时间戳 -> 字幕分段 ----------

_STRONG_PUNCT = "。！？；…"
_WEAK_PUNCT = "，、,;：:）)"
_PUNCT_RE = re.compile(r"[\s，。！？；、,.!?;:：…（）()\"'“”‘’\-—]")


def _display_width(text):
    """字幕显示宽度：CJK/全角标点计 1，其余（英文数字）计 0.55。"""
    w = 0.0
    for ch in text:
        w += 1.0 if ord(ch) > 0x2E80 else 0.55
    return w


def _norm_word(text):
    """词文本归一化：去空白与标点，用于跨块同一词的判重。"""
    return _PUNCT_RE.sub("", text or "")


def _result_words(result):
    """从 API 响应提取词级时间戳。

    verbose_json + timestamp_granularities=word 时返回
    words: [{word, start, end}, ...]；个别情况下没有 words 字段，
    退化为把 segments 当整词（一条字幕=一段）。
    """
    words = result.get("words")
    if isinstance(words, list) and words:
        return [{"word": w.get("word") or "", "start": float(w.get("start") or 0),
                 "end": float(w.get("end") or 0)} for w in words]
    segs = result.get("segments") or []
    return [{"word": s.get("text") or "", "start": float(s.get("start") or 0),
             "end": float(s.get("end") or 0)} for s in segs]


def _merge_words(all_words):
    """合并各块 words（已加全局 offset）：保持文本顺序，文本+时间双重重叠判重。

    重叠切块会在交界处把同一句子转写两遍，词序列几乎一致：归一化文本相同
    且时间区间显著重叠（>较短者一半）即判为同一词，保留一个并展宽时间范围
    （两侧时间戳取并集，边界更稳）。只在一块出现的词原样保留=漏识补缺。

    注意：all_words 是"块序 + 块内模型输出序"，也就是正确的文本顺序，
    这里绝不可按时间排序。Whisper 给每个词的时间戳是独立回归的，相邻词
    经常倒挂 0.2s 上下，一排序正确字序就被打乱（见 _realign_words 注释）。
    """
    ws = [w for w in all_words if _norm_word(w.get("word"))]
    kept = []
    for w in ws:
        dup_of = None
        for j in range(len(kept) - 1, max(-1, len(kept) - 10), -1):
            k = kept[j]
            if _norm_word(k["word"]) != _norm_word(w["word"]):
                continue
            ov = min(k["end"], w["end"]) - max(k["start"], w["start"])
            shorter = min(k["end"] - k["start"], w["end"] - w["start"])
            if ov > 0.25 and shorter > 0 and ov > shorter * 0.5:
                dup_of = j
                break
        if dup_of is None:
            kept.append(dict(w))
        else:  # 同一词的两份识别：时间范围取并集
            k = kept[dup_of]
            k["start"] = min(k["start"], w["start"])
            k["end"] = max(k["end"], w["end"])
    return kept


def _ends_with_filler(text):
    """末尾是口播水词（嗯/啊/呃…）？用于跳过水词处的气口断句。"""
    t = _PUNCT_RE.sub("", text or "")
    return bool(t) and t[-1] in _FILLERS


def _explode_long_words(words, max_chars):
    """把超宽的单个"词"二次切开，时间按显示宽度比例分配。

    Groq 偶尔只返回 segments 不给 words（或个别 chunk 的粒度异常粗），
    此时一个"词"就是整段几十上百字——不切开会生成一条几十秒的巨型字幕
    （实测旧草稿里出现过 223 字 / 59 秒的单条）。切分优先落在标点上，
    其次按宽度硬切，时间按字数比例摊到各片。
    """
    out = []
    for w in words:
        t = (w.get("word") or "").strip()
        if _display_width(t) <= max_chars * 1.6:
            out.append(w)
            continue
        # 先按标点切成小块
        parts, buf = [], ""
        for ch in t:
            buf += ch
            if ch in _STRONG_PUNCT or ch in _WEAK_PUNCT:
                parts.append(buf)
                buf = ""
        if buf:
            parts.append(buf)
        # 过宽的块再按显示宽度硬切
        pieces = []
        for pt in parts:
            while _display_width(pt) > max_chars:
                acc, cut = 0.0, len(pt)
                for idx, ch in enumerate(pt):
                    acc += _display_width(ch)
                    if acc > max_chars:
                        cut = max(1, idx)
                        break
                pieces.append(pt[:cut])
                pt = pt[cut:]
            if pt:
                pieces.append(pt)
        total = sum(_display_width(p) for p in pieces) or 1.0
        s0, e0 = w.get("start", 0), w.get("end", 0)
        acc = 0.0
        for k, p in enumerate(pieces):
            pw = _display_width(p)
            nw = {"word": p,
                  "start": round(s0 + (e0 - s0) * acc / total, 3),
                  "end": round(s0 + (e0 - s0) * (acc + pw) / total, 3)}
            if k == 0 and w.get("brk"):
                nw["brk"] = True
            out.append(nw)
            acc += pw
    return out


def _sanitize_words(words, max_dur):
    """修正 Whisper 的畸形词时间戳，并强制全局单调。

    实测《众神》6452 个词里有 2 个畸形：
        '这' 435.35-442.31（单字跨 6.96s，把后面十几个词整个盖住）
        '一' 598.08-600.15（单字跨 2.07s）
    正常语速约 4~5 字/秒，单字跨度 0.2s 上下；"1 个字占 7 秒"只可能是 Whisper
    在重复/循环片段上的时间戳错乱。不修会造成两处可见缺陷：
        1) 该词所在字幕条被拉到 7 秒，字却只有一个
        2) 后续词的 start 落在它内部 -> 与上一条重叠 -> 防重叠硬钳 -> 闪读条
    修法：跨度异常且字数很少的词，end 收到 start + SPAN_FIX（不越过下一词
    start）；再整体保证 start >= 上一词 end，消除词间重叠。
    """
    span_limit = min(SPAN_MAX, max_dur)
    n = len(words)
    out = []
    for i, w in enumerate(words):
        s = float(w.get("start", 0.0))
        e = float(w.get("end", s))
        chars = _display_width(w.get("word") or "")
        if e - s > span_limit and chars <= 4:
            cap = s + SPAN_FIX
            if i + 1 < n:
                cap = min(cap, float(words[i + 1].get("start", cap)))
            e = max(s + 0.10, cap)
        nw = dict(w)
        nw["start"], nw["end"] = s, e
        out.append(nw)

    prev_end = None
    for w in out:
        if prev_end is not None and w["start"] < prev_end:
            w["start"] = prev_end
        if w["end"] <= w["start"]:
            w["end"] = round(w["start"] + 0.08, 3)
        prev_end = w["end"]
    return out


def _brk_ahead(words, i, look=3, limit_w=16.0):
    """前方 look 个词内有没有气口，有的话累计宽度是否还在 limit_w 内。

    "气口"= 该词带 brk 标记（音频对齐测出的语音区间起点，即它前面有停顿）。
    用来支撑"宽度将超限时先别切、等前方气口"的前瞻判断。
    """
    acc = 0.0
    for j in range(i + 1, min(i + 1 + look, len(words))):
        acc += _display_width((words[j].get("word") or "").strip())
        if words[j].get("brk"):
            return acc <= limit_w
    return False


_ZH_WORDS = None


def _load_wordset():
    """加载中文词表（与脚本同目录的 zh_words.txt）。缺失时返回空集合，
    所有切点约束自动失效，退化成"只按宽度断"的旧行为，不影响可用性。"""
    global _ZH_WORDS
    if _ZH_WORDS is None:
        cand = []
        try:
            cand.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "zh_words.txt"))
        except Exception:
            pass
        cand.append(os.path.join(os.getcwd(), "zh_words.txt"))
        if getattr(sys, "_MEIPASS", None):
            cand.insert(0, os.path.join(sys._MEIPASS, "zh_words.txt"))
        ws = frozenset()
        for p in cand:
            try:
                with open(p, encoding="utf-8") as f:
                    ws = frozenset(x.strip() for x in f if x.strip())
                break
            except Exception:
                continue
        _ZH_WORDS = ws
    return _ZH_WORDS


def _cut_flags(text, wordset=None, maxlen=4):
    """算出每个位置能不能断开，返回长度 len(text)+1 的布尔表。

    做法是正向 + 逆向两次最大匹配，取交集。单用一种方向会被分词歧义骗到：
    "自然|而人为" 逆向会切成 "自" + "然而" + "人为"，于是把 "然|而" 判成
    词内不可切，实际应该允许（这是"自然"的尾巴）。两个方向都认为是词内部的
    位置才禁切，误判就消掉了。

    词表缺失或空文本时全 True（不做约束，退化成旧行为）。
    """
    n = len(text)
    ws = _load_wordset() if wordset is None else wordset
    if not ws or n == 0:
        return [True] * (n + 1)

    def forward():
        ok = [False] * (n + 1)
        ok[0] = True
        i = 0
        while i < n:
            hit = False
            for L in range(min(maxlen, n - i), 1, -1):
                if text[i:i + L] in ws:
                    ok[i + L] = True
                    i += L
                    hit = True
                    break
            if not hit:
                ok[i + 1] = True
                i += 1
        return ok

    def backward():
        ok = [False] * (n + 1)
        ok[n] = True
        i = n
        while i > 0:
            hit = False
            for L in range(min(maxlen, i), 1, -1):
                if text[i - L:i] in ws:
                    ok[i - L] = True
                    i -= L
                    hit = True
                    break
            if not hit:
                ok[i - 1] = True
                i -= 1
        return ok

    f, b = forward(), backward()
    return [x and y for x, y in zip(f, b)]


def _split_cues(words, max_chars=CUE_MAX_CHARS, max_dur=CUE_MAX_DUR,
                min_dur=CUE_MIN_DUR, gap=BREAK_GAP):
    """把词级时间戳切成适合屏幕阅读的字幕条。

    断开优先级（从高到低）：
    0. 真实气口——词带 brk 标记（音频对齐测出的语音区间起点），见之即断
    1. 句末标点（。！？；…）——句子说完就断
    2. 气口——词间静音 >= gap 秒（说话人换气/停顿；无对齐时的退化判据）
    3. 逗号级标点（，、：等）——累计宽度过半才断，避免碎句
    4. 兜底——累计宽度/时长将超上限时，在最近的逗号处切；没有就硬切
    每条 cue: start=首词 start，end=末词 end + 余韵 CUE_TAIL，text=拼接。
    """
    # 防御：把超宽的巨型词先切开（Groq 未返回 words 时的退化路径）
    words = _explode_long_words(words, max_chars)
    words = [w for w in words if (w.get("word") or "").strip()]
    # 再修时间戳畸形（单字跨好几秒）并强制单调，见 _sanitize_words
    words = _sanitize_words(words, max_dur)
    # 词边界约束：切点必须落在中文词边界上。Whisper 给的是逐字时间戳，没有
    # 词边界信息，光按宽度切必然把词劈开（实测 683 个切点里 141 个劈在词内，
    # 20.6%：大英博|物馆、力|量、保|命、野|蛮、横|跨）。用词表做一次逆向最大
    # 匹配，把每个字的"可切"标记算出来，后面所有切点都从这里选。
    _texts = [(w.get("word") or "").strip() for w in words]
    _offs, _acc = [], 0
    for _t in _texts:
        _offs.append(_acc)
        _acc += len(_t)
    _okcut = _cut_flags("".join(_texts))

    def can_cut(gi):
        """第 gi 个词之前能不能断（gi=0 是段首，gi=len(words) 是段尾）"""
        if gi <= 0 or gi >= len(_offs):
            return True
        return _okcut[_offs[gi]]
    cues = []
    cur = []  # [(word, start, end), ...]

    def text_of(items):
        return "".join(t for t, *_ in items)

    def flush(items=None):
        nonlocal cur
        if items is None:
            items = cur
            cur = []
        if not items:
            return
        txt = text_of(items).strip()
        if txt:
            cues.append({"start": round(items[0][1], 3),
                         "end": round(items[-1][2] + CUE_TAIL, 3), "text": txt})

    for wi, w in enumerate(words):
        t = (w.get("word") or "").strip()
        ws, we = float(w.get("start") or 0), float(w.get("end") or 0)
        if we < ws:  # 防御：个别词 end 缺失/倒挂
            we = ws
        if cur:
            prev_end = cur[-1][2]
            silence = ws - prev_end
            cur_w = _display_width(text_of(cur))
            would_w = cur_w + _display_width(t)
            # 0) 真实气口：音频对齐判定此处有停顿，且已有足够内容才断；
            #    但上一条停在"嗯/啊"这类水词上时不切（口播换气不该单独成条）
            if w.get("brk") and cur_w >= 4 and not _ends_with_filler(text_of(cur)):
                flush()
            # 1) 气口断开（无 brk 标记时的退化判据）
            elif silence >= gap and cur_w >= 4:
                flush()
            # 2) 软断点：短停顿(0.25~气口阈值) + 标点 + 已积够半数宽度，三条
            #    信号叠加到阈值以上才切——单看任何一条都不够（剪映的加权思路）
            elif (silence >= SOFT_GAP
                  and any(c in t for c in _STRONG_PUNCT + _WEAK_PUNCT)
                  and cur_w >= max_chars * 0.5):
                flush()
            # 3.5) 前瞻：宽度将超限，但前方 3 个词内就有气口，且等到那个气口
            #      也还在 1.5 倍上限内——就等它，别在这里硬切。
            #      依据：剪映原生 21% 的条落在 13~16 字、最长正好 16 字，说明它
            #      不是按固定宽度切，而是"一口气没换气就让它读完，读完再说"。
            #      我们死守 11 字硬上限，13 字以上一条都没有，这是唯一剩下的
            #      分布差距。
            elif (would_w > max_chars
                  and (we - cur[0][1]) <= max_dur * 0.8
                  and _brk_ahead(words, wi, 3, max_chars * 1.5)):
                pass
            # 4a) 宽度/时长兜底：先找 cue 内最近的逗号切点
            elif would_w > max_chars or (we - cur[0][1]) > max_dur:
                cut = -1
                for j in range(len(cur) - 1, -1, -1):
                    ct = cur[j][0]
                    if any(c in ct for c in _WEAK_PUNCT) and j >= 2:
                        cut = j
                        break
                if cut >= 0:
                    head, cur = cur[:cut + 1], cur[cut + 1:]
                    flush(head)
                elif (any(c in t for c in _WEAK_PUNCT)
                      and would_w <= max_chars * 1.25):
                    # 断点正好落在标点上：允许小幅超宽，别把词劈开
                    cur.append((t, ws, we, wi))
                    flush()
                    continue
                else:
                    # 4b) 回看找呼吸点：累计宽度将超限、又不在标点上时，
                    #     不要就地硬切——Whisper 常给逐字时间戳，"词"已退化
                    #     成单字，就地切必然切在字中间（实测把"好书"劈成
                    #     好|书，而原生在"值→解"那 0.21s 换气处断）。
                    #     回头找 cur 内间隔最大的位置，那里才是真正的气口。
                    base = cur[0][3]
                    best_j, best_gap = -1, 0.0
                    for j in range(1, len(cur)):
                        if not can_cut(base + j):   # 词内不可切
                            continue
                        g = cur[j][1] - cur[j - 1][2]
                        if g > best_gap:
                            best_gap, best_j = g, j
                    head_w = (_display_width(text_of(cur[:best_j]))
                              if best_j > 0 else 0)
                    if (best_j > 0 and best_gap >= LOOKBACK_GAP
                            and head_w >= MIN_CUE_WIDTH):
                        head, cur = cur[:best_j], cur[best_j:]
                        flush(head)
                    else:
                        # 没有明显气口：退到最靠后的词边界，尽量多装字，
                        # 但绝不切在词内部（宁可这条短一点，也别出现保|命）
                        j2 = 0
                        for j in range(len(cur) - 1, 0, -1):
                            if can_cut(base + j):
                                j2 = j
                                break
                        if j2 > 0 and _display_width(text_of(cur[:j2])) >= 2:
                            head, cur = cur[:j2], cur[j2:]
                            flush(head)
                        else:
                            flush()
        cur.append((t, ws, we, wi))
        # 2) 句末标点：本词收尾整句
        if any(c in t for c in _STRONG_PUNCT):
            if _display_width(text_of(cur)) >= STRONG_MIN_CHARS or len(cur) > 1:
                flush()
        # 3) 逗号级：累计过半才断
        elif any(c in t for c in _WEAK_PUNCT) \
                and _display_width(text_of(cur)) >= max_chars * WEAK_BREAK_RATIO:
            flush()
    flush()

    # 孤儿条合并：宽度不足 MIN_CUE_WIDTH 的碎条并入上一条（避免 1-3 字闪读），
    # 前提是合并后不超宽。宁可上一条多停 0.2s，也别闪一屏 2 个字。
    if len(cues) > 1:
        kept = [cues[0]]
        for c in cues[1:]:
            p = kept[-1]
            # 只在"又短又快"且前面没有大停顿时才合并：
            # 文本窄 + 停留不足 min_dur = 闪读；若前面已有 >= BREAK_GAP 的
            # 大停顿则保持独立（宁可短一点，也不要让上一条跨过气口）。
            too_short = (_display_width(c["text"]) < MIN_CUE_WIDTH
                         and (c["end"] - c["start"]) < min_dur)
            small_gap = (c["start"] - p["end"]) < BREAK_GAP
            if (too_short and small_gap
                    and _display_width(p["text"]) + _display_width(c["text"])
                    <= max_chars):
                p["text"] += c["text"]
                p["end"] = max(p["end"], c["end"])
            else:
                kept.append(c)
        cues = kept

    # 时长下限 + 防重叠：太短的条拉长到 min_dur；任何情况下都不许压住下一条
    for i, c in enumerate(cues):
        nxt = cues[i + 1]["start"] if i + 1 < len(cues) else None
        if c["end"] - c["start"] < min_dur:
            c["end"] = c["start"] + min_dur
        if nxt is not None and c["end"] > nxt - 0.02:
            # 这里必须是 min 而不是 max(start+0.3, ...)：下一条紧邻时，
            # "至少 0.3 秒"的兜底会重新越过它，让两条字幕叠在一起
            # （实测《众神》464.4s 处三条 0.2s 碎条互压）。宁可这一条短，
            # 也不能压住下一条——重叠在剪映里是肉眼可见的糊字。
            c["end"] = min(c["end"], nxt - 0.02)
    # 被压到几乎没有时长的碎条：文字并进上一条，避免闪读又丢字
    merged = []
    for c in cues:
        if merged and (c["end"] - c["start"]) < 0.05:
            p = merged[-1]
            if _display_width(p["text"]) + _display_width(c["text"]) <= max_chars:
                p["text"] += c["text"]
                p["end"] = max(p["end"], c["end"])
                continue
        merged.append(c)
    return merged


def _parse_db(text, key):
    m = re.search(key + r":\s*(-?\d+(?:\.\d+)?)\s*dB", text or "")
    return float(m.group(1)) if m else None


def _parse_silences(text):
    """从 silencedetect 的输出里解析静音区间 [(start, end), ...]。"""
    out, cur = [], None
    for line in (text or "").splitlines():
        m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m:
            cur = max(0.0, float(m.group(1)))
            continue
        m = re.search(r"silence_end:\s*(-?[\d.]+)", line)
        if m and cur is not None:
            out.append((cur, float(m.group(1))))
            cur = None
    if cur is not None:          # 一直静到文件结尾
        out.append((cur, float("inf")))
    return out


def detect_speech_regions(audio_path, ffmpeg, log=None,
                          min_sil=SILENCE_MIN_DUR, min_speech=SPEECH_MIN_DUR):
    """从波形直接测出真实说话区间，返回 [(start, end), ...]（秒）。

    先 volumedetect 拿整体响度，据此自适应定噪声门限（均值 -8dB，夹在
    -45..-26dB），再 silencedetect 找静音段，取补集即语音段。探测不到任何
    静音（底噪大/门限过高）时降 6dB 重试一次。

    这份区间是从音频量出来的地面真值——Whisper 的时间戳是模型"推断"的，
    遇到 segment 内长停顿会整体偏移，必须用它来纠正。
    """
    def run_detect(noise_db):
        af = "silencedetect=noise=%ddB:d=%.2f" % (int(round(noise_db)), min_sil)
        r = _run([ffmpeg, "-hide_banner", "-nostats", "-i", audio_path,
                  "-af", af, "-f", "null", "-"],
                 capture_output=True, text=True,
                 encoding="utf-8", errors="ignore")
        return r.stderr or ""

    def probe_mean():
        r = _run([ffmpeg, "-hide_banner", "-nostats", "-i", audio_path,
                  "-af", "volumedetect", "-f", "null", "-"],
                 capture_output=True, text=True,
                 encoding="utf-8", errors="ignore")
        return _parse_db(r.stderr or "", "mean_volume")

    mean_db = probe_mean()
    noise_db = -35.0 if mean_db is None else max(-45.0, min(-26.0, mean_db - 8.0))
    silences = _parse_silences(run_detect(noise_db))
    if not silences:
        alt = _parse_silences(run_detect(noise_db - 6.0))
        if alt:
            silences, noise_db = alt, noise_db - 6.0
    if not silences:
        return []

    total = m4a_end_time(audio_path) or 0.0
    regions, pos = [], 0.0
    for s, e in silences:
        e = min(e, total) if total else e
        if s - pos >= min_speech:
            regions.append((pos, s))
        pos = max(pos, e)
    if total and total - pos >= min_speech:
        regions.append((pos, total))
    if log:
        log("[..] 静音检测: 门限 %ddB，%d 段停顿 -> %d 段语音"
            % (int(round(noise_db)), len(silences), len(regions)))
    return regions


def _realign_words(words, regions):
    """把词时间戳钉到真实语音区间上，并在每个气口打上断句标记。

    1) 每个词归到与之重叠最多（无重叠则中心最近）的语音区间；
    2) 强制区间序号单调不减（词序不可逆，避免交叉）；
    3) 同区间的词按 Whisper 给的相对位置线性重映射到该区间的真实起止；
    4) 区间首词打 brk=True —— 这就是"气口"，_split_cues 见之即断。

    结果：词不会落在静音里，字幕起止天然对上气口。

    ⚠️ 这里绝不能用时间戳重排词序。Whisper 的 words 数组是模型按文本顺序
    输出、再给每个词单独回归一个时间戳；相邻词的时间戳经常重叠甚至倒挂
    （实测《众神》：「《」比前一个「创」晚 start 0.16s、却比「作」早 0.18s，
    「简」比「世」早 0.26s，「让」单字跨 0.92s）。一旦 sort(start)：
        他此前创作的《大英博物馆世界简史》风靡全球
    会被排成
        他此前创《作的大英 | 博物馆简世界》史风靡 | 全让球无数读者
    音一个字都没错，纯粹是排序把字搬了家——这是最容易被误判成"ASR 精度差"
    的一类缺陷。所以：保序 + 只把时间轴单调化。
    """
    if not words or not regions:
        return words
    ws = [dict(w) for w in words]
    # 统计时间戳倒挂程度，仅用于日志（不做任何重排）
    backwards = sum(1 for a, b in zip(ws, ws[1:])
                    if float(b.get("start", 0)) < float(a.get("start", 0)) - 1e-6)
    prev_s = None
    for w in ws:                       # 保序单调化：start 只前推，不改变词序
        s = float(w.get("start", 0.0))
        e = float(w.get("end", s))
        if prev_s is not None and s < prev_s:
            s = prev_s
        if e < s + 0.02:
            e = s + 0.02
        w["start"], w["end"] = s, e
        prev_s = s
    n = len(regions)
    idx = []
    for w in ws:
        s, e = w["start"], w["end"]
        best, best_ov = -1, 0.0
        for k, (rs, re_) in enumerate(regions):
            ov = min(e, re_) - max(s, rs)
            if ov > best_ov:
                best_ov, best = ov, k
        if best < 0:
            c = (s + e) / 2.0
            best = min(range(n), key=lambda k: abs(
                (regions[k][0] + regions[k][1]) / 2.0 - c))
        idx.append(best)
    for i in range(1, len(idx)):
        if idx[i] < idx[i - 1]:
            idx[i] = idx[i - 1]

    out, i, prev_k = [], 0, None
    while i < len(ws):
        k = idx[i]
        j = i
        while j < len(ws) and idx[j] == k:
            j += 1
        grp = ws[i:j]
        rs, re_ = regions[k]
        span = max(re_ - rs, 0.02)
        # 组内投影一律按显示宽度铺开，不再用 Whisper 的词时间戳。
        # 原因：词时间戳本身抖动 ±0.2s（见函数头注释），补扫插进来的词还会
        # 与原有词大范围重叠；一旦按时间比例映射，某个词 end 偏大就会把后面
        # 所有词顶到区间末尾，压出一堆 0.1 秒的闪读条（实测 7 条）。
        # 而区间是 VAD 量出来的"连续说话段"，段内语速基本均匀，按字数分配
        # 反而最稳：同一区间里每个字的时长一致，天然单调、不重叠。
        widths = [max(_display_width((g.get("word") or "").strip()), 1) for g in grp]
        tot = sum(widths) or 1
        prev_ne = rs
        acc = 0.0
        for g, gw in zip(grp, widths):
            ns = rs + span * acc / tot
            acc += gw
            ne = rs + span * acc / tot
            # 溢出区间边界就钳回来
            ns = min(max(ns, rs), re_)
            ne = min(max(ne, ns), re_)
            # 保序单调：词序已固定，时间不许倒挂；被顶到前面就顺延，
            # 但必须留出可见时长，否则会出现 0 时长的闪读条
            if ns < prev_ne:
                ns = prev_ne
            if ns > re_ - 0.01:
                ns = max(rs, re_ - 0.01)
            if ne < ns + 0.01:
                ne = min(re_, ns + 0.01)
            prev_ne = ne
            out.append({"word": g["word"], "start": round(ns, 3),
                        "end": round(ne, 3)})
        # 区间首词 = 气口断点；但只有真正拉开距离（>= ALIGN_BREAK_GAP）才断，
        # 0.35~0.4s 的句中小换气不算气口，硬断会把句子切碎
        gap_before = regions[k][0] - regions[prev_k][1] if prev_k is not None else 0.0
        out[-len(grp)]["brk"] = prev_k is not None and gap_before >= ALIGN_BREAK_GAP
        prev_k = k
        i = j
    return out


def _align_words(words, audio_path, ffmpeg, log=None, enabled=ALIGN_TO_SPEECH):
    """按音频真实语音区间校正词时间戳；失败时原样返回（不阻断转录）。"""
    if not enabled or not ffmpeg or not words:
        return words
    try:
        regions = detect_speech_regions(audio_path, ffmpeg, log=log)
    except Exception as e:                       # noqa: BLE001 探测失败不影响主流程
        if log:
            log("[warn] 音频对齐失败（%s），沿用 Whisper 时间戳" % e)
        return words
    if not regions:
        if log:
            log("[warn] 未探测到语音区间，沿用 Whisper 时间戳")
        return words
    backwards = sum(1 for a, b in zip(words, words[1:])
                    if float(b.get("start", 0)) < float(a.get("start", 0)) - 1e-6)
    out = _realign_words(words, regions)
    moved = sum(1 for a, b in zip(words, out)
                if abs(float(a.get("start", 0)) - float(b.get("start", 0))) > 0.15)
    if log:
        log("[ok] 音频对齐: %d 段语音，%d/%d 个词时间被校正(>0.15s)，"
            "气口 %d 处%s" % (len(regions), moved, len(out),
                             sum(1 for w in out if w.get("brk")),
                             ("；检测到 %d 处时间戳倒挂，已按文本顺序处理"
                              % backwards) if backwards else ""))
    return out


def _refill_gaps(words, regions, audio_path, ffmpeg, key, model, language,
                 proxy=None, log=print):
    """补扫 Whisper 块内跳读：把"语速密度异常低"的语音段切出来重识别并替换。

    症状：某段语音 6 秒钟只识别出 1 个字，而音频里明明说了一整句。
    成因是 Whisper 在长音频里会成段"跳过"，prompt/温度都压不住。
    探针：以全片中位字/秒为基准，低于 GAP_DENSITY_RATIO 或整段 0 字即可疑。
    修法：可疑段（前后扩 GAP_PAD 秒）单独切片重新转写，只取落在该段内的词，
    替换掉原来的低质量词；补扫结果不比原来多字就丢弃，绝不倒退。

    返回新的 words（保持文本顺序），任何失败都原样返回。
    """
    if not words or not regions or not ffmpeg:
        return words

    n = len(regions)

    def region_of(t):
        for k in range(n):
            if regions[k][0] <= t <= regions[k][1]:
                return k
        return -1

    counts = [0] * n
    for w in words:
        k = region_of((float(w["start"]) + float(w["end"])) / 2.0)
        if k >= 0:
            counts[k] += len(w["word"])
    durs = [max(regions[k][1] - regions[k][0], 0.01) for k in range(n)]
    dens = [counts[k] / durs[k] for k in range(n)]
    valid = [dens[k] for k in range(n) if durs[k] >= 1.0 and counts[k] > 0]
    if len(valid) < 5:
        return words
    med = statistics.median(valid)

    bad = [k for k in range(n)
           if durs[k] >= GAP_MIN_DUR
           and (counts[k] == 0 or dens[k] < GAP_DENSITY_RATIO * med)]
    if not bad:
        log("[ok] 漏读补扫: 未发现异常段（中位密度 %.2f 字/秒）" % med)
        return words

    blocks = []                       # 连续可疑段并成一块，减少上传次数
    for k in bad:
        if blocks and k == blocks[-1][1] + 1:
            blocks[-1][1] = k
        else:
            blocks.append([k, k])

    log("[..] 漏读补扫: %d 段可疑（中位密度 %.2f 字/秒），合并为 %d 块"
        % (len(bad), med, len(blocks)))

    tmpdir = tempfile.mkdtemp(prefix="refill_")
    patches = []                      # [(t0, t1, [word...])]
    try:
        for bi, (k0, k1) in enumerate(blocks):
            t0, t1 = float(regions[k0][0]), float(regions[k1][1])
            s = max(0.0, t0 - GAP_PAD)
            e = t1 + GAP_PAD
            seg = os.path.join(tmpdir, "g%d.m4a" % bi)
            r = subprocess.run(
                [ffmpeg, "-y", "-v", "error", "-ss", "%.3f" % s,
                 "-t", "%.3f" % (e - s), "-i", audio_path,
                 "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "64k", seg],
                capture_output=True, text=True)
            if r.returncode != 0:
                log("[warn] 补扫切片失败 %.1f-%.1f" % (t0, t1))
                continue
            try:
                nw = _result_words(_upload_one(seg, key, model, language, proxy, log))
            except Exception as ex:                  # noqa: BLE001 补扫不阻断主流程
                log("[warn] 补扫识别失败 %.1f-%.1f（%s）" % (t0, t1, ex))
                continue
            keep = []
            for w in nw:
                ws_ = float(w.get("start", 0)) + s
                we_ = float(w.get("end", 0)) + s
                if we_ < t0 - 0.05 or ws_ > t1 + 0.05:
                    continue                          # 落在扩边里，归邻块管
                keep.append({"word": w["word"], "start": ws_, "end": we_})
            old = sum(counts[k] for k in range(k0, k1 + 1))
            new = sum(len(x["word"]) for x in keep)
            if new <= old:
                log("[warn] 补扫 %.1f-%.1f 没取回更多文本（%d -> %d 字），保留原样"
                    % (t0, t1, old, new))
                continue
            patches.append((t0, t1, keep))
            log("[ok] 补扫 %.1f-%.1f: %d -> %d 字" % (t0, t1, old, new))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if not patches:
        return words

    patches.sort(key=lambda p: p[0])
    dropped = set(bad)
    out, pi = [], 0
    for w in words:
        c = (float(w["start"]) + float(w["end"])) / 2.0
        while pi < len(patches) and c > patches[pi][1]:
            out.extend(patches[pi][2])
            pi += 1
        if region_of(c) in dropped:                   # 旧的低质量词由补扫结果代替
            continue
        out.append(w)
    while pi < len(patches):
        out.extend(patches[pi][2])
        pi += 1
    return out


def _upload_one(audio_path, key, model, language, proxy, log, prompt=None):
    boundary = "----groqsrt" + os.urandom(16).hex()
    with open(audio_path, "rb") as f:
        file_bytes = f.read()
    filename = os.path.basename(audio_path)

    def field(name, value):
        return ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                % (boundary, name, value)).encode("utf-8")

    body = b""
    body += field("model", model)
    body += field("response_format", "verbose_json")
    # 词级时间戳：segment 级一段可达几十秒，字幕会堆成一大块；
    # 词级才能按标点/气口智能分段（_split_cues）
    body += field("timestamp_granularities[]", "word")
    if language:
        body += field("language", language)
    if prompt:
        body += field("prompt", prompt)
    body += ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
             "Content-Type: audio/mp4\r\n\r\n" % (boundary, filename)).encode("utf-8")
    body += file_bytes
    body += ("\r\n--%s--\r\n" % boundary).encode("utf-8")

    try:
        status, raw = _request(
            "POST", API_PATH, key, proxy, body=body,
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary,
                     "Content-Length": str(len(body))})
    except NetError as e:
        raise RuntimeError(str(e))
    if status != 200:
        _die_for_status(status, raw.decode("utf-8", "replace"))
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise RuntimeError("响应解析失败（%s）。重试一次，持续失败请换代理节点。"
                           % type(e).__name__)


def transcribe(audio_path, key, model, language, proxy, log, ffmpeg, stop_flag,
               vocab=None, use_prompt=True, overlap=OVERLAP_SEC,
               max_chars=CUE_MAX_CHARS, max_dur=CUE_MAX_DUR,
               align=ALIGN_TO_SPEECH):
    """转录入口：小文件直接传；>4MB 自动切块（块间重叠 overlap 秒）逐块传。

    流程：词级时间戳 -> 跨块合并去重（_merge_words）-> 音频对齐
    （_align_words：用真实语音区间校正 Whisper 漂移，并在气口打标记）->
    智能分段（_split_cues）-> 返回 segments 形式的 cues（start/end/text）。

    vocab: 引导词表（正确写法，list 或分隔字符串）——注入 Whisper prompt，
            从事后正则热词变成解码期引导，专治同音词漂移。
    use_prompt=False 时完全禁用 prompt（含上文续接）。
    """
    vocab_text = build_vocab_text(vocab)
    size = os.path.getsize(audio_path)
    if size <= CHUNK_THRESHOLD:
        log("[..] 上传 %.1f MB 到 Groq（代理 %s）..." % (size / 1048576.0, proxy))
        prompt = build_prompt(vocab_text, None) if use_prompt else None
        if prompt:
            log("[..] 注入识别引导: %s" % prompt[:60])
        result = _upload_one(audio_path, key, model, language, proxy, log,
                             prompt=prompt)
        words = _result_words(result)
        if not words:
            raise RuntimeError("API 未返回词级时间戳")
        words = _align_words(words, audio_path, ffmpeg, log=log, enabled=align)
        return {"segments": _split_cues(words, max_chars, max_dur)}

    n_chunks = -(-size // CHUNK_THRESHOLD)
    log("[..] %.1f MB 超过单传阈值，切 %d 块（重叠 %.1fs）逐块上传..."
        % (size / 1048576.0, n_chunks, overlap))
    tmpdir = tempfile.mkdtemp(prefix="subtitle-wb-chunks-")
    try:
        chunks = split_audio(audio_path, tmpdir, n_chunks, ffmpeg, overlap=overlap)
        all_words = []
        prev_tail = ""
        for i, (fp, offset) in enumerate(chunks, 1):
            if stop_flag.is_set():
                raise RuntimeError("任务已被用户中止")
            log("[..] 块 %d/%d：%.1f MB（偏移 +%.0fs）..."
                % (i, len(chunks), os.path.getsize(fp) / 1048576.0, offset))
            prompt = build_prompt(vocab_text, prev_tail) if use_prompt else None
            if prompt:
                log("[..] 注入识别引导: %s" % prompt[:60])
            result = _upload_one(fp, key, model, language, proxy, log,
                                 prompt=prompt)
            words = _result_words(result)
            if not words:
                raise RuntimeError("块 %d 未返回词级时间戳" % i)
            for w in words:
                w["start"] = w.get("start", 0) + offset
                w["end"] = w.get("end", 0) + offset
                all_words.append(w)
            # 本块全部文本的结尾，作为下一块的 prompt 上文（跨切块不断片）
            prev_tail = "".join((w.get("word") or "").strip()
                                for w in words)[-PROMPT_TAIL_CHARS:]
            log("[ok] 块 %d/%d 完成，累计 %d 个词" % (i, len(chunks), len(all_words)))
        merged = _merge_words(all_words)
        if len(merged) < len(all_words):
            log("[ok] 重叠区去重: %d -> %d 个词" % (len(all_words), len(merged)))
        regions = []
        if align and ffmpeg:
            try:
                regions = detect_speech_regions(audio_path, ffmpeg, log=log)
            except Exception as e:            # noqa: BLE001 探测失败不影响主流程
                log("[warn] 音频对齐失败（%s），沿用 Whisper 时间戳" % e)
        if regions:
            backwards = sum(1 for x, y in zip(merged, merged[1:])
                            if float(y.get("start", 0)) < float(x.get("start", 0)) - 1e-6)
            merged = _realign_words(merged, regions)
            log("[ok] 音频对齐: %d 段语音，气口 %d 处%s"
                % (len(regions), sum(1 for w in merged if w.get("brk")),
                   ("；检测到 %d 处时间戳倒挂，已按文本顺序处理" % backwards)
                   if backwards else ""))
            # 块内跳读补扫：测出语速密度异常低的语音段，切片重识别后替换
            before = len(merged)
            merged = _refill_gaps(merged, regions, audio_path, ffmpeg, key,
                                  model, language, proxy, log=log)
            if len(merged) != before:
                merged = _realign_words(merged, regions)   # 补扫后重新钉时间/气口
                log("[ok] 漏读补扫完成: %d -> %d 个词" % (before, len(merged)))
        else:
            merged = _align_words(merged, audio_path, ffmpeg, log=log, enabled=align)
        cues = _split_cues(merged, max_chars, max_dur)
        log("[ok] 智能分段: %d 个词 -> %d 条字幕" % (len(merged), len(cues)))
        return {"segments": cues}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==================== 热词纠错 + SRT ====================

def parse_hotwords(text):
    """每行一条: 错别字=正确词（或 错别字->正确词）。# 开头为注释。"""
    pairs = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(.+?)\s*(?:=>|->|→|=)\s*(.+)$", line)
        if m:
            wrong, right = m.group(1).strip(), m.group(2).strip()
            if wrong:
                pairs.append((wrong, right))
    return pairs


def apply_hotwords(blocks_text, pairs, log):
    if not pairs:
        return blocks_text
    total = 0
    for wrong, right in pairs:
        n = blocks_text.count(wrong)
        if n:
            blocks_text = blocks_text.replace(wrong, right)
            total += n
    if total:
        log("[ok] 热词纠错: %d 处替换（%d 条规则命中）"
            % (total, sum(1 for w, _ in pairs if w in blocks_text) or len(pairs)))
    else:
        log("[..] 热词纠错: 无需替换")
    return blocks_text


# ==================== 处理管线（工作线程） ====================

def process_one(audio, cfg, log, stop_flag, preflighted):
    """处理单个音频。返回 SRT 路径。"""
    if not os.path.isfile(audio):
        raise RuntimeError("文件不存在: " + audio)
    key = (cfg.get("key") or "").strip()
    if not key:
        raise RuntimeError("未设置 Groq API Key，请先填写并保存")
    if key.startswith("xai-"):
        raise RuntimeError("这是 xAI(Grok) 的 key。Groq 的是 gsk_ 开头，去 console.groq.com 创建")
    proxy = cfg.get("proxy") or DEFAULT_PROXY
    if proxy.lower() in ("none", "direct", "off"):
        proxy = None

    ffmpeg = find_ffmpeg(cfg.get("ffmpeg"))
    out = os.path.splitext(audio)[0] + ".srt"
    tmp = os.path.splitext(out)[0] + ".groq.m4a"

    log("[..] 转码: %s" % os.path.basename(audio))
    size = transcode(audio, tmp, ffmpeg)
    log("[ok] 转码完成 %.1f MB（ffmpeg=%s）" % (size / 1048576.0, ffmpeg))
    if size > SIZE_LIMIT:
        raise RuntimeError("超过 25MB 上限（%.1f MB）" % (size / 1048576.0))

    if not preflighted[0]:
        preflight(key, proxy, log)
        preflighted[0] = True

    # 热词表的"正确写法"同时用作 Whisper prompt 引导词（解码期就倾向正确词），
    # 事后正则纠错仍然保留，双保险
    vocab = [right for _, right in parse_hotwords(cfg.get("hotwords"))]
    result = transcribe(tmp, key, cfg.get("model") or "whisper-large-v3",
                        cfg.get("language") or "zh", proxy, log, ffmpeg, stop_flag,
                        vocab=vocab, use_prompt=cfg.get("prompt_boost", True),
                        overlap=cfg.get("overlap", OVERLAP_SEC),
                        max_chars=cfg.get("cue_max_chars", CUE_MAX_CHARS),
                        max_dur=cfg.get("cue_max_dur", CUE_MAX_DUR),
                        align=cfg.get("align_to_speech", ALIGN_TO_SPEECH))
    segs = result.get("segments") or []
    if not segs:
        raise RuntimeError("API 未返回字幕")

    blocks = []
    for i, seg in enumerate(segs, 1):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        blocks.append("%d\n%s --> %s\n%s\n"
                      % (i, srt_ts(seg.get("start", 0)), srt_ts(seg.get("end", 0)), text))
    text_all = "\n".join(blocks)
    text_all = apply_hotwords(text_all, parse_hotwords(cfg.get("hotwords")), log)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text_all)
    try:
        os.remove(tmp)
    except OSError:
        pass
    n = text_all.count("\n\n") // 2 + text_all.count(" --> ")
    log("[ok] SRT 已生成: %s（约 %d 条）" % (out, len(blocks)))
    return out


# ==================== 剪映草稿生成（子进程调 from_srt.py） ====================

def find_python_exe(explicit=None):
    """找一个能跑 from_srt.py 的真实 Python 解释器。

    非 frozen（脚本运行）时 sys.executable 就是；
    PyInstaller onefile 下 sys.executable 是 exe 本身，不接受脚本参数，
    必须探测机器上的 Python（用户装剪映工作台前本机就有 Python）。
    """
    if not getattr(sys, "frozen", False):
        return sys.executable
    cands = []
    if explicit:
        cands.append(explicit)
    local = os.environ.get("LOCALAPPDATA", "")
    for v in ("315", "314", "313", "312", "311", "310", "39", "38"):
        cands.append(r"C:/Python%s/python.exe" % v)
        if local:
            cands.append(os.path.join(local, "Programs", "Python",
                                      "Python" + v, "python.exe"))
    from shutil import which
    for name in ("python", "python3", "py"):
        c = which(name)
        if c:
            cands.append(c)
    for c in cands:
        if c and os.path.isfile(c):
            try:
                _run([c, "--version"], capture_output=True, timeout=15)
                return c
            except Exception:
                continue
    return None


def find_jianying_exe():
    """定位 JianyingPro.exe（探测顺序与 jianying-subtitle-gui/config.py 对齐）。"""
    cands = [r"D:/Jianying6.01/Jianying_Pro/JianyingPro.exe",
             r"C:/Program Files/JianyingPro/JianyingPro.exe",
             r"C:/Program Files (x86)/JianyingPro/JianyingPro.exe",
             r"D:/Program Files/JianyingPro/JianyingPro.exe",
             r"D:/JianyingPro/JianyingPro.exe",
             r"C:/JianyingPro/JianyingPro.exe"]
    cands += glob.glob(r"D:/Jianying*/Jianying_Pro/JianyingPro.exe")
    cands += glob.glob(r"C:/Jianying*/Jianying_Pro/JianyingPro.exe")
    cands += glob.glob(r"E:/Jianying*/Jianying_Pro/JianyingPro.exe")
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def jianying_running():
    try:
        p = _run(["tasklist", "/FI", "IMAGENAME eq JianyingPro.exe"],
                 capture_output=True, text=True, timeout=10,
                 encoding="utf-8", errors="ignore")
        return "JianyingPro.exe" in (p.stdout or "")
    except Exception:
        return False


def launch_jianying(exe):
    """启动剪映。DETACHED_PROCESS：本工具退出后剪照常运行。"""
    flags = subprocess.CREATE_NEW_PROCESS_GROUP
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags |= subprocess.DETACHED_PROCESS
    flags |= _NO_WINDOW
    subprocess.Popen([exe], cwd=os.path.dirname(exe),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags)


def run_draft_gen(audio, srt, cfg, log, stop_flag):
    """调 from_srt.py 把 SRT + 音频写成剪映草稿，完成后打开剪映。

    子进程方式：from_srt.py 属于 jianying-subtitle-gui 仓库（复用其验证过的
    jianying.py 草稿写入器），本 GUI 不 import 它，只通过 CLI 调用——
    解耦、崩溃互不影响、单文件零依赖的定位不变。
    """
    script = cfg.get("from_srt_script") or DEFAULT_FROM_SRT
    if not os.path.isfile(script):
        raise RuntimeError("找不到 from_srt.py: %s\n"
                           "（在设置里改「草稿生成脚本」路径，或确认 "
                           "jianying-subtitle-gui 仓库已克隆）" % script)
    py = find_python_exe(cfg.get("python_exe"))
    if not py:
        raise RuntimeError(
            "找不到 Python 解释器（exe 版需要它来运行草稿生成脚本）。\n"
            "  装一个 Python 3.9+，或在 config.json 里配置 \"python_exe\"。")
    exe = find_jianying_exe()
    if not exe:
        raise RuntimeError("找不到剪映程序 JianyingPro.exe")
    if not os.path.isfile(srt):
        raise RuntimeError("SRT 不存在: %s" % srt)

    if jianying_running():
        log("[ warn] 剪映正在运行 → 将自动关闭以保护草稿索引"
            "（你的草稿会自动保存，重开后恢复）")

    name = os.path.splitext(os.path.basename(audio))[0]
    cmd = [py, script, audio, srt, "--no-launch", "--name", name]
    if cfg.get("draft_fix"):
        cmd.append("--fix")
    log("[..] 生成剪映草稿「%s」…" % name)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1,
                            cwd=os.path.dirname(script),
                            creationflags=_NO_WINDOW)
    tail = []

    def reader():
        try:
            for ln in proc.stdout:
                ln = ln.rstrip()
                tail.append(ln)
                if ln:
                    log("      " + ln)
        except Exception:
            pass
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    while t.is_alive():
        if stop_flag.is_set():
            proc.terminate()
            raise RuntimeError("草稿生成已中止")
        t.join(0.2)
    rc = proc.wait()
    if stop_flag.is_set():
        raise RuntimeError("草稿生成已中止")
    if rc != 0:
        detail = "\n".join(tail[-6:]) or "（无输出）"
        raise RuntimeError("草稿生成失败（exit %d）:\n%s" % (rc, detail))
    log("[ok] 草稿已生成: %s" % name)
    launch_jianying(exe)
    log("[ok] 剪映已打开，草稿箱里即是「%s」" % name)
    return True


# ==================== 拖放支持（Windows 原生 WM_DROPFILES，纯 ctypes） ====================

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _WM_DROPFILES = 0x0233
    _GWL_WNDPROC = -4
    _WNDPROC_T = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
        ctypes.c_size_t, ctypes.c_ssize_t)
    _drop_keepalive = []  # 持引用，防止 WNDPROC 回调被 GC

    def _drag_query_files(hdrop):
        """从 HDROP 取出全部文件路径（DragQueryFileW，中文路径安全）。"""
        shell32 = ctypes.windll.shell32
        shell32.DragQueryFileW.restype = wintypes.UINT
        shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                           wintypes.LPWSTR, wintypes.UINT]
        n = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
        out = []
        for i in range(n):
            buf = ctypes.create_unicode_buffer(1024)
            if shell32.DragQueryFileW(hdrop, i, buf, len(buf)):
                out.append(buf.value)
        return out

    def enable_drag_drop(root, handler):
        """让 root 窗口接收资源管理器拖入的文件/文件夹。

        handler(paths: list[str]) 在 Tk 主线程（消息循环内）被调用。
        原理：子类化 Tk 顶层窗口的 WndProc，自己处理 WM_DROPFILES，
        其余消息透传给 Tk 原窗口过程。
        """
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        user32.GetParent.restype = wintypes.HWND
        user32.GetParent.argtypes = [wintypes.HWND]
        set_wlp = getattr(user32, "SetWindowLongPtrW", None) \
            or user32.SetWindowLongW
        set_wlp.restype = ctypes.c_ssize_t
        set_wlp.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.CallWindowProcW.restype = ctypes.c_ssize_t
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                                           ctypes.c_uint, ctypes.c_size_t,
                                           ctypes.c_ssize_t]
        shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        shell32.DragFinish.argtypes = [wintypes.HANDLE]

        tk_hwnd = int(root.winfo_id())
        hwnd = user32.GetParent(tk_hwnd) or tk_hwnd
        shell32.DragAcceptFiles(hwnd, True)

        old = [None]

        def wndproc(h, msg, wp, lp):
            if msg == _WM_DROPFILES:
                try:
                    files = _drag_query_files(wp)
                except Exception:
                    files = []
                finally:
                    shell32.DragFinish(wp)
                if files:
                    try:
                        handler(files)
                    except Exception:
                        pass
                return 0
            return user32.CallWindowProcW(old[0], h, msg, wp, lp)

        proc = _WNDPROC_T(wndproc)
        old[0] = set_wlp(hwnd, _GWL_WNDPROC, proc)
        _drop_keepalive.append((root, proc))
        return hwnd

else:
    def enable_drag_drop(root, handler):
        return None


# ==================== GUI ====================

HELP_TEXT = """两种用法，按需选择：

【A. 全自动（推荐）】勾选「生成剪映草稿」
  音频 -> SRT -> 剪映草稿，全程零手工。生成后剪映自动打开，
  草稿箱里就是带音频轨 + 字幕轨的可编辑工程。

【B. 手动导入】不勾选「生成剪映草稿」时
  1. 打开剪映 →「开始创作」新建草稿（或打开你正在做的工程）
  2. 把音频文件拖进「素材区」，再拖到下方时间轴
     （音频文件名带《》：等符号，可在本工具点「复制音频路径」，
       在剪映文件对话框里 Ctrl+V 快速定位）
  3. 顶部菜单点「文本」→ 左侧「本地字幕」→「导入」
  4. 选择刚生成的 .srt 文件 → 字幕自动按时间码落轨
  5. 点任意字幕条可改字；Ctrl+A 全选字幕条可统一改字体/字号/颜色

通用提示：
- 添加音频：点「＋ 添加音频」，或把文件/文件夹直接拖进本窗口（文件夹会递归找音视频）
- SRT 为 UTF-8 无 BOM，时间码与原音频严格对齐
- 生成草稿前若剪映在运行，会自动关闭它（否则剪映退出时会用内存索引
  覆盖新草稿的注册条目）——你的草稿已自动保存，重开即恢复
- 心理学专有名词识别不准？把「错别字=正确词」填进热词纠错框再生成
- 草稿生成走 jianying-subtitle-gui 的 from_srt.py（设置里可改路径）
"""


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.log_queue = queue.Queue()
        self.stop_flag = threading.Event()
        self.worker = None
        self.results = {}  # audio -> srt path
        self.preflighted = [False]

        root.title("有声书字幕工作台")
        root.geometry("920x760")
        root.minsize(760, 600)

        pad = {"padx": 10, "pady": 4}
        frm = ttk.Frame(root)
        frm.pack(fill="both", expand=True, padx=8, pady=8)

        # ---- 设置区 ----
        s1 = ttk.LabelFrame(frm, text=" Groq 设置 ")
        s1.pack(fill="x", **pad)
        ttk.Label(s1, text="API Key:").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.key_var = tk.StringVar(value=self.cfg.get("key", ""))
        ttk.Entry(s1, textvariable=self.key_var, width=58, show="•").grid(
            row=0, column=1, columnspan=3, sticky="we", padx=6)
        ttk.Button(s1, text="保存 Key", command=self.save_settings).grid(
            row=0, column=4, padx=6)
        ttk.Label(s1, text="模型:").grid(row=1, column=0, sticky="w", padx=6)
        self.model_var = tk.StringVar(value=self.cfg.get("model", "whisper-large-v3"))
        ttk.Combobox(s1, textvariable=self.model_var, width=24, state="readonly",
                     values=["whisper-large-v3", "whisper-large-v3-turbo"]).grid(
            row=1, column=1, sticky="w", padx=6)
        ttk.Label(s1, text="语言:").grid(row=1, column=2, sticky="e")
        self.lang_var = tk.StringVar(value=self.cfg.get("language", "zh"))
        ttk.Entry(s1, textvariable=self.lang_var, width=8).grid(
            row=1, column=3, sticky="w", padx=6)
        self.key_state = ttk.Label(s1, text="", foreground="#666")
        self.key_state.grid(row=1, column=4, padx=6)
        s1.columnconfigure(1, weight=1)

        # ---- 剪映草稿选项 ----
        s1b = ttk.LabelFrame(frm, text=" 剪映草稿 ")
        s1b.pack(fill="x", **pad)
        self.gen_draft_var = tk.BooleanVar(value=bool(self.cfg.get("gen_draft", True)))
        ttk.Checkbutton(s1b, text="生成剪映草稿（SRT + 音频 → 草稿箱，自动打开剪映）",
                        variable=self.gen_draft_var).grid(
            row=0, column=0, columnspan=5, sticky="w", padx=6, pady=(6, 2))
        ttk.Label(s1b, text="脚本:").grid(row=1, column=0, sticky="w", padx=6)
        self.fromsrt_var = tk.StringVar(
            value=self.cfg.get("from_srt_script") or DEFAULT_FROM_SRT)
        ttk.Entry(s1b, textvariable=self.fromsrt_var, width=52).grid(
            row=1, column=1, columnspan=3, sticky="we", padx=6)
        ttk.Button(s1b, text="浏览…", command=self.pick_fromsrt).grid(
            row=1, column=4, padx=6)
        self.draft_fix_var = tk.BooleanVar(value=bool(self.cfg.get("draft_fix")))
        ttk.Checkbutton(s1b, text="字幕术语清洗（去标点，配合 resegment 规范）",
                        variable=self.draft_fix_var).grid(
            row=2, column=0, columnspan=5, sticky="w", padx=6, pady=(0, 6))
        self.fromsrt_state = ttk.Label(s1b, text="", foreground="#666")
        self.fromsrt_state.grid(row=2, column=4, padx=6, sticky="e")
        s1b.columnconfigure(1, weight=1)

        # ---- 音频队列 ----
        s2 = ttk.LabelFrame(frm, text=" 音频队列（点「＋ 添加音频」，或把文件/文件夹直接拖进来） ")
        s2.pack(fill="x", **pad)
        btns = ttk.Frame(s2)
        btns.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Button(btns, text="＋ 添加音频", command=self.add_files).pack(side="left")
        ttk.Button(btns, text="－ 移除选中", command=self.remove_selected).pack(
            side="left", padx=8)
        ttk.Button(btns, text="清空", command=self.clear_all).pack(side="left")
        self.listbox = tk.Listbox(s2, height=5, selectmode="extended",
                                  font=("Microsoft YaHei UI", 9))
        self.listbox.pack(fill="x", padx=6, pady=6)
        self.listbox.bind("<Double-Button-1>", lambda e: self.copy_audio_path())
        enable_drag_drop(root, self.on_drop_files)

        # ---- 操作区 ----
        s3 = ttk.Frame(frm)
        s3.pack(fill="x", **pad)
        self.start_btn = ttk.Button(s3, text="▶  开始生成字幕", command=self.start)
        self.start_btn.pack(side="left")
        self.draft_btn = ttk.Button(s3, text="✂  从 SRT 生成草稿",
                                    command=self.gen_draft_from_list)
        self.draft_btn.pack(side="left", padx=8)
        self.stop_btn = ttk.Button(s3, text="■  中止", command=self.stop,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(s3, text="打开输出文件夹", command=self.open_outdir).pack(
            side="left", padx=8)
        ttk.Button(s3, text="复制音频路径", command=self.copy_audio_path).pack(
            side="left")
        ttk.Button(s3, text="剪映导入教程", command=self.show_help).pack(
            side="right")

        # ---- 热词纠错 ----
        s4 = ttk.LabelFrame(frm, text=" 热词（每行一条: 错别字=正确词）"
                                   "  → 识别时注入 Whisper 引导 + 识别后正则纠错 ")
        s4.pack(fill="x", **pad)
        self.hot_txt = scrolledtext.ScrolledText(s4, height=4, wrap="word",
                                                 font=("Consolas", 9))
        self.hot_txt.pack(fill="x", padx=6, pady=6)
        self.hot_txt.insert("1.0", self.cfg.get("hotwords", ""))
        self.prompt_boost_var = tk.BooleanVar(
            value=bool(self.cfg.get("prompt_boost", True)))
        ttk.Checkbutton(s4, text="识别引导（热词注入 Whisper prompt + 跨切块上文续接，"
                                 "切块重叠 3 秒）",
                        variable=self.prompt_boost_var).pack(
            anchor="w", padx=6, pady=(0, 2))
        self.align_var = tk.BooleanVar(
            value=bool(self.cfg.get("align_to_speech", ALIGN_TO_SPEECH)))
        ttk.Checkbutton(s4, text="音频对齐（测音频真实停顿来校正 Whisper 的时间漂移，"
                                 "字幕从气口起、到气口止；关掉=完全信任识别时间戳）",
                        variable=self.align_var).pack(
            anchor="w", padx=6, pady=(0, 6))

        # ---- 字幕分段：单行宽度 ----
        row = ttk.Frame(s4)
        row.pack(anchor="w", padx=6, pady=(0, 6))
        ttk.Label(row, text="单行最多字数（汉字计 1、英文计 0.5；"
                            "按屏幕宽度与气口自动断句）:").pack(side="left")
        self.cue_chars_var = tk.IntVar(
            value=int(self.cfg.get("cue_max_chars", CUE_MAX_CHARS) or CUE_MAX_CHARS))
        ttk.Spinbox(row, from_=6, to=30, width=4, textvariable=self.cue_chars_var
                    ).pack(side="left", padx=6)

        # ---- 日志 ----
        s5 = ttk.LabelFrame(frm, text=" 日志 ")
        s5.pack(fill="both", expand=True, **pad)
        self.log_txt = scrolledtext.ScrolledText(s5, height=10, wrap="word",
                                                 font=("Consolas", 9),
                                                 state="disabled")
        self.log_txt.pack(fill="both", expand=True, padx=6, pady=6)

        # ---- 状态栏 ----
        self.status = ttk.Label(frm, text="就绪", foreground="#666")
        self.status.pack(fill="x", **pad)

        self.refresh_key_state()
        self.refresh_fromsrt_state()
        self.root.after(120, self.poll_log)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- 工具 ----------
    def log(self, msg):
        self.log_queue.put(("log", msg))

    def poll_log(self):
        try:
            while True:
                kind, msg = self.log_queue.get_nowait()
                if kind == "log":
                    self._append_log(msg)
                elif kind == "status":
                    self.status.config(text=msg)
                elif kind == "done":
                    self._on_worker_done(msg)
                elif kind == "item":
                    self._mark_item(msg)
        except queue.Empty:
            pass
        self.root.after(120, self.poll_log)

    def _append_log(self, msg):
        self.log_txt.config(state="normal")
        self.log_txt.insert("end", msg + "\n")
        self.log_txt.see("end")
        self.log_txt.config(state="disabled")

    def refresh_key_state(self):
        has = bool((self.key_var.get() or "").strip())
        self.key_state.config(
            text=("✓ 已保存" if has and self.key_var.get() == self.cfg.get("key")
                  else ("已填写，记得保存" if has else "未设置")))

    def refresh_fromsrt_state(self):
        p = (self.fromsrt_var.get() or "").strip()
        if not p:
            self.fromsrt_state.config(text="", foreground="#666")
        elif os.path.isfile(p):
            self.fromsrt_state.config(text="✓ 脚本就绪", foreground="#0a7d2c")
        else:
            self.fromsrt_state.config(text="✗ 路径不存在", foreground="#b00020")

    def pick_fromsrt(self):
        f = filedialog.askopenfilename(
            title="选择 from_srt.py（jianying-subtitle-gui 仓库内）",
            filetypes=[("Python", "*.py"), ("所有文件", "*.*")])
        if f:
            self.fromsrt_var.set(f)
            self.refresh_fromsrt_state()

    # ---------- 动作 ----------
    def save_settings(self):
        self.cfg["key"] = self.key_var.get().strip()
        self.cfg["model"] = self.model_var.get()
        self.cfg["language"] = self.lang_var.get().strip() or "zh"
        self.cfg["hotwords"] = self.hot_txt.get("1.0", "end").rstrip("\n")
        self.cfg["gen_draft"] = bool(self.gen_draft_var.get())
        self.cfg["draft_fix"] = bool(self.draft_fix_var.get())
        self.cfg["prompt_boost"] = bool(self.prompt_boost_var.get())
        self.cfg["align_to_speech"] = bool(self.align_var.get())
        try:
            self.cfg["cue_max_chars"] = max(6, min(40, int(self.cue_chars_var.get())))
        except (TypeError, ValueError):
            self.cfg["cue_max_chars"] = CUE_MAX_CHARS
        self.cfg["from_srt_script"] = self.fromsrt_var.get().strip()
        if save_config(self.cfg):
            self.refresh_key_state()
            self.refresh_fromsrt_state()
            self._append_log("[ok] 设置已保存到 %s" % CONFIG_PATH)
        else:
            messagebox.showerror("保存失败", "无法写入 %s" % CONFIG_PATH)

    def add_files(self):
        files = filedialog.askopenfilenames(
            title="选择音频/视频文件（可多选）",
            filetypes=[("音视频", " ".join("*" + e for e in AUDIO_EXTS)),
                       ("所有文件", "*.*")])
        for f in files:
            if f not in self.results and f not in self.listbox.get(0, "end"):
                self.listbox.insert("end", f)

    def remove_selected(self):
        for i in reversed(self.listbox.curselection()):
            f = self.listbox.get(i)
            self.results.pop(f, None)
            self.listbox.delete(i)

    def clear_all(self):
        self.listbox.delete(0, "end")
        self.results.clear()

    def on_drop_files(self, paths):
        """资源管理器拖入：文件按扩展名过滤，文件夹递归找音视频。"""
        added = [0]
        skipped = [0]
        existing = set(self.listbox.get(0, "end"))

        def try_add(fp):
            fp = os.path.normpath(fp)
            if fp in existing:
                return
            base = os.path.basename(fp)
            for item in existing:  # 已完成过的（列表里是「✓ ...」）不重复加
                if item.startswith("✓ ") and base in item:
                    return
            existing.add(fp)
            self.listbox.insert("end", fp)
            added[0] += 1

        for p in paths:
            p = os.path.normpath(p)
            if os.path.isdir(p):
                for dirpath, _dirs, files in os.walk(p):
                    for fn in sorted(files):
                        if os.path.splitext(fn)[1].lower() in AUDIO_EXTS:
                            try_add(os.path.join(dirpath, fn))
            elif os.path.splitext(p)[1].lower() in AUDIO_EXTS:
                try_add(p)
            else:
                skipped[0] += 1
        if added[0]:
            self.log("[ok] 拖入添加 %d 个文件（跳过 %d 个不支持的）"
                     % (added[0], skipped[0]))
        elif skipped[0]:
            self.log("[warn] 拖入的 %d 个文件不是音视频格式（支持 %s 等）"
                     % (skipped[0], "/".join(AUDIO_EXTS[:6])))

    def start(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务在进行中")
            return
        items = list(self.listbox.get(0, "end"))
        if not items:
            messagebox.showinfo("提示", "请先添加音频文件")
            return
        self.cfg["key"] = self.key_var.get().strip()
        self.cfg["model"] = self.model_var.get()
        self.cfg["language"] = self.lang_var.get().strip() or "zh"
        self.cfg["hotwords"] = self.hot_txt.get("1.0", "end").rstrip("\n")
        self.cfg["gen_draft"] = bool(self.gen_draft_var.get())
        self.cfg["draft_fix"] = bool(self.draft_fix_var.get())
        self.cfg["prompt_boost"] = bool(self.prompt_boost_var.get())
        self.cfg["align_to_speech"] = bool(self.align_var.get())
        try:
            self.cfg["cue_max_chars"] = max(6, min(40, int(self.cue_chars_var.get())))
        except (TypeError, ValueError):
            self.cfg["cue_max_chars"] = CUE_MAX_CHARS
        self.cfg["from_srt_script"] = self.fromsrt_var.get().strip()
        if not self.cfg["key"]:
            messagebox.showwarning("缺少 Key",
                                   "请先填写 Groq API Key（console.groq.com 创建，gsk_ 开头）")
            return
        if self.cfg["gen_draft"]:
            script = self.cfg["from_srt_script"] or DEFAULT_FROM_SRT
            if not os.path.isfile(script):
                messagebox.showwarning(
                    "草稿生成脚本缺失",
                    "找不到 from_srt.py:\n%s\n\n"
                    "请确认 jianying-subtitle-gui 仓库已克隆，或在"
                    "「剪映草稿」区修改脚本路径。" % script)
                return
        save_config(self.cfg)
        self.stop_flag.clear()
        self.preflighted[0] = False
        self.start_btn.config(state="disabled")
        self.draft_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.worker = threading.Thread(target=self._run, args=(items,), daemon=True)
        self.worker.start()

    def gen_draft_from_list(self):
        """对列表选中项：找同目录同名 .srt，直接生成剪映草稿（不跑 ASR）。"""
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务在进行中")
            return
        sel = self.listbox.curselection()
        if not sel:
            messagebox.showinfo("提示",
                                "先在列表里选中一个音频\n（同目录下需有同名 .srt 文件）")
            return
        item = self.listbox.get(sel[0])
        audio = item
        if item.startswith("✓ "):
            for a in self.results:
                if os.path.basename(a) in item:
                    audio = a
                    break
        srt = os.path.splitext(audio)[0] + ".srt"
        if not os.path.isfile(srt):
            messagebox.showwarning("缺少 SRT", "未找到:\n%s" % srt)
            return
        self.cfg["draft_fix"] = bool(self.draft_fix_var.get())
        self.cfg["prompt_boost"] = bool(self.prompt_boost_var.get())
        self.cfg["align_to_speech"] = bool(self.align_var.get())
        try:
            self.cfg["cue_max_chars"] = max(6, min(40, int(self.cue_chars_var.get())))
        except (TypeError, ValueError):
            self.cfg["cue_max_chars"] = CUE_MAX_CHARS
        self.cfg["from_srt_script"] = self.fromsrt_var.get().strip()
        save_config(self.cfg)
        self.stop_flag.clear()
        self.start_btn.config(state="disabled")
        self.draft_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.worker = threading.Thread(target=self._run_drafts,
                                       args=([(audio, srt)],), daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_flag.set()
        self.log("[..] 已请求中止（当前块传完即停）...")

    def _run(self, items):
        ok = 0
        fail = 0
        for i, audio in enumerate(items, 1):
            if self.stop_flag.is_set():
                break
            self.log_queue.put(("status", "处理中 %d/%d: %s"
                               % (i, len(items), os.path.basename(audio))))
            self.log("\n===== [%d/%d] %s =====" % (i, len(items), os.path.basename(audio)))
            try:
                srt = process_one(audio, self.cfg, self.log, self.stop_flag,
                                  self.preflighted)
                self.results[audio] = srt
                self.log_queue.put(("item", audio))
                if self.cfg.get("gen_draft"):
                    run_draft_gen(audio, srt, self.cfg, self.log, self.stop_flag)
                ok += 1
            except Exception as e:
                self.log("[error] %s" % e)
                fail += 1
        self.log_queue.put(("done", (ok, fail)))

    def _run_drafts(self, pairs):
        ok = 0
        fail = 0
        for audio, srt in pairs:
            if self.stop_flag.is_set():
                break
            self.log_queue.put(("status", "生成草稿: %s"
                               % os.path.basename(audio)))
            self.log("\n===== 草稿: %s =====" % os.path.basename(audio))
            try:
                run_draft_gen(audio, srt, self.cfg, self.log, self.stop_flag)
                ok += 1
            except Exception as e:
                self.log("[error] %s" % e)
                fail += 1
        self.log_queue.put(("done", (ok, fail)))

    def _mark_item(self, audio):
        srt = self.results.get(audio)
        try:
            i = list(self.listbox.get(0, "end")).index(audio)
            self.listbox.delete(i)
            self.listbox.insert(i, "✓ %s  →  %s" % (os.path.basename(audio),
                                                    os.path.basename(srt or "")))
            self.listbox.itemconfig(i, foreground="#0a7d2c")
        except ValueError:
            pass

    def _on_worker_done(self, counts):
        ok, fail = counts
        self.start_btn.config(state="normal")
        self.draft_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        if self.stop_flag.is_set():
            self.log_queue.put(("status", "已中止（完成 %d，失败 %d）" % (ok, fail)))
        elif fail == 0:
            self.log_queue.put(("status", "全部完成 ✓ 共 %d 个" % ok))
            if self.cfg.get("gen_draft"):
                self._append_log(
                    "\n[下一步] 剪映已打开，草稿箱里就是刚生成的工程"
                    "（音频轨 + 字幕轨，每条字幕可双击改字）")
            else:
                self._append_log(
                    "\n[下一步] 打开剪映 → 文本 → 本地字幕 → 导入，选刚生成的 .srt")
        else:
            self.log_queue.put(("status", "完成 %d 个，失败 %d 个" % (ok, fail)))

    def open_outdir(self):
        sel = self.listbox.curselection()
        target = None
        if sel:
            item = self.listbox.get(sel[0])
            if item.startswith("✓ "):
                for a, s in self.results.items():
                    if os.path.basename(a) in item:
                        target = os.path.dirname(s)
                        break
            else:
                target = os.path.dirname(item)
        if not target:
            target = BASE_DIR
        try:
            os.startfile(target)
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    def copy_audio_path(self):
        sel = self.listbox.curselection()
        item = self.listbox.get(sel[0]) if sel else None
        path = item
        if item and item.startswith("✓ "):
            for a in self.results:
                if os.path.basename(a) in item:
                    path = a
                    break
        if not path:
            messagebox.showinfo("提示", "先在列表里选中一个文件")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(path)
        self._append_log("[ok] 已复制路径: %s" % path)

    def show_help(self):
        win = tk.Toplevel(self.root)
        win.title("剪映导入 SRT 教程")
        win.geometry("640x480")
        txt = scrolledtext.ScrolledText(win, wrap="word", padx=12, pady=12,
                                        font=("Microsoft YaHei UI", 10))
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", HELP_TEXT)
        txt.config(state="disabled")
        ttk.Button(win, text="关闭", command=win.destroy).pack(pady=8)

    def on_close(self):
        self.cfg["hotwords"] = self.hot_txt.get("1.0", "end").rstrip("\n")
        self.cfg["gen_draft"] = bool(self.gen_draft_var.get())
        self.cfg["draft_fix"] = bool(self.draft_fix_var.get())
        self.cfg["prompt_boost"] = bool(self.prompt_boost_var.get())
        self.cfg["align_to_speech"] = bool(self.align_var.get())
        try:
            self.cfg["cue_max_chars"] = max(6, min(40, int(self.cue_chars_var.get())))
        except (TypeError, ValueError):
            self.cfg["cue_max_chars"] = CUE_MAX_CHARS
        self.cfg["from_srt_script"] = self.fromsrt_var.get().strip()
        save_config(self.cfg)
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
