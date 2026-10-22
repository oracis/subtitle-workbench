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
DEFAULT_PROXY = "socks5h://127.0.0.1:10808"
DEFAULT_FROM_SRT = r"D:/jianying-subtitle-gui/app/from_srt.py"

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
           "from_srt_script": DEFAULT_FROM_SRT}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
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


def split_audio(src, out_dir, n_chunks, ffmpeg):
    total = m4a_end_time(src)
    if not total:
        raise RuntimeError("无法解析音频时长，切块失败。")
    seg_time = total / n_chunks
    pattern = os.path.join(out_dir, "seg%03d.m4a")
    cmd = [ffmpeg, "-y", "-v", "error", "-i", src,
           "-f", "segment", "-segment_time", "%.3f" % seg_time,
           "-c", "copy", pattern]
    r = _run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg 切块失败:\n" + (r.stderr or "")[:500])
    files = sorted(glob.glob(os.path.join(out_dir, "seg*.m4a")))
    chunks = []
    prev_end = 0.0
    for fp in files:
        if os.path.getsize(fp) < 10240:
            continue  # 跳过尾部空 segment
        end = m4a_end_time(fp)
        chunks.append((fp, prev_end))
        prev_end = end if end else prev_end + seg_time
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


def _upload_one(audio_path, key, model, language, proxy, log):
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
    body += field("timestamp_granularities[]", "segment")
    if language:
        body += field("language", language)
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


def transcribe(audio_path, key, model, language, proxy, log, ffmpeg, stop_flag):
    size = os.path.getsize(audio_path)
    if size <= CHUNK_THRESHOLD:
        log("[..] 上传 %.1f MB 到 Groq（代理 %s）..." % (size / 1048576.0, proxy))
        return _upload_one(audio_path, key, model, language, proxy, log)

    n_chunks = -(-size // CHUNK_THRESHOLD)
    log("[..] %.1f MB 超过单传阈值，切 %d 块逐块上传..."
        % (size / 1048576.0, n_chunks))
    tmpdir = tempfile.mkdtemp(prefix="subtitle-wb-chunks-")
    try:
        chunks = split_audio(audio_path, tmpdir, n_chunks, ffmpeg)
        merged = []
        for i, (fp, offset) in enumerate(chunks, 1):
            if stop_flag.is_set():
                raise RuntimeError("任务已被用户中止")
            log("[..] 块 %d/%d：%.1f MB（偏移 +%.0fs）..."
                % (i, len(chunks), os.path.getsize(fp) / 1048576.0, offset))
            result = _upload_one(fp, key, model, language, proxy, log)
            segs = result.get("segments") or []
            if not segs:
                raise RuntimeError("块 %d 未返回段落" % i)
            for seg in segs:
                seg["start"] = seg.get("start", 0) + offset
                seg["end"] = seg.get("end", 0) + offset
                merged.append(seg)
            log("[ok] 块 %d/%d 完成，累计 %d 条" % (i, len(chunks), len(merged)))
        return {"segments": merged}
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

    result = transcribe(tmp, key, cfg.get("model") or "whisper-large-v3",
                        cfg.get("language") or "zh", proxy, log, ffmpeg, stop_flag)
    segs = result.get("segments") or []
    if not segs:
        raise RuntimeError("API 未返回段落")

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
        s4 = ttk.LabelFrame(frm, text=" 热词纠错（可选，每行一条: 错别字=正确词） ")
        s4.pack(fill="x", **pad)
        self.hot_txt = scrolledtext.ScrolledText(s4, height=4, wrap="word",
                                                 font=("Consolas", 9))
        self.hot_txt.pack(fill="x", padx=6, pady=6)
        self.hot_txt.insert("1.0", self.cfg.get("hotwords", ""))

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
        self.cfg["from_srt_script"] = self.fromsrt_var.get().strip()
        save_config(self.cfg)
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
