#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
学习状态监测系统 · 手机拍照测试工具（本地网页版）

用途：ESP32-CAM 还没烧录时，用手机当摄像头，拍到照片直接发到云端 /detect
接口，当场看到 AI 判定结果（头高 / 肩平差 / 是否端正），用于阈值标定。

依赖：只用 Python 标准库，无需安装任何第三方包。

启动：
    python capture_server.py
然后手机连同一个 WiFi，浏览器打开启动时打印的那个网址。
"""

from __future__ import annotations

import json
import socket
import sys
import urllib.error
import urllib.request
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

SERVER_DETECT = "http://YOUR_SERVER_IP:8000/detect"
DEVICE_KEY = "CHANGE_ME_DEVICE_KEY"
LISTEN_PORT = 8765
MAX_UPLOAD = 12 * 1024 * 1024  # 单张上限 12MB，防止手机原图过大


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>坐姿检测 · 拍照测试</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; padding: 16px; background: #f4f5f7; color: #1a1a1a; }
  h1 { font-size: 18px; margin: 0 0 12px; }
  .card { background: #fff; border-radius: 12px; padding: 16px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 14px; }
  .row { display: flex; gap: 10px; flex-wrap: wrap; }
  label.btn { flex: 1; min-width: 130px; text-align: center; padding: 14px 10px;
              border-radius: 10px; background: #1677ff; color: #fff;
              font-size: 15px; font-weight: 600; cursor: pointer; }
  label.btn.alt { background: #6b7280; }
  input[type=file] { display: none; }
  #status { margin-top: 12px; font-size: 14px; color: #666; min-height: 20px; }
  img.preview { width: 100%; border-radius: 10px; margin-top: 12px; display: none; }
  .verdict { font-size: 30px; font-weight: 700; text-align: center;
             padding: 14px 0; border-radius: 10px; margin-bottom: 14px; }
  .ok   { background: #e8f8ef; color: #0d8a4f; }
  .bad  { background: #fdecec; color: #d02b2b; }
  .unk  { background: #f1f2f4; color: #5a6270; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  td { padding: 8px 4px; border-bottom: 1px solid #eef0f3; }
  td:first-child { color: #666; width: 45%; }
  td:last-child { font-weight: 600; font-variant-numeric: tabular-nums; }
  pre { background: #1f2430; color: #d7dce5; padding: 12px; border-radius: 8px;
        overflow-x: auto; font-size: 12px; margin: 0; }
  .hint { font-size: 13px; color: #7a8291; line-height: 1.6; margin-top: 8px; }
  a.back { display: inline-block; margin-top: 14px; color: #1677ff;
           text-decoration: none; font-size: 15px; }
</style>
</head>
<body>
<h1>坐姿检测 · 拍照测试</h1>

<div class="card">
  <div class="row">
    <label class="btn" for="cam">拍照</label>
    <label class="btn alt" for="lib">从相册选</label>
  </div>
  <input type="file" id="cam" accept="image/*" capture="environment">
  <input type="file" id="lib" accept="image/*">
  <div id="status">选好照片后自动上传检测…</div>
  <img class="preview" id="preview">
</div>

<div class="card">
  <div class="hint">
    <b>拍照位置要和 ESP32-CAM 一致</b>：桌上正对面、平视、离头约 0.5~0.6m。<br>
    姿势：<b>先坐直拍一张</b>看数值，再<b>刻意低头驼背拍一张</b>，对比头高变化。
  </div>
</div>

<script>
const cam = document.getElementById('cam');
const lib = document.getElementById('lib');
const statusEl = document.getElementById('status');
const preview = document.getElementById('preview');

async function upload(file) {
  if (!file) return;
  statusEl.textContent = '上传中… (' + Math.round(file.size/1024) + ' KB)';

  const url = URL.createObjectURL(file);
  preview.src = url;
  preview.style.display = 'block';

  const fd = new FormData();
  fd.append('image', file, file.name || 'photo.jpg');

  try {
    const t0 = performance.now();
    const resp = await fetch('/detect', { method: 'POST', body: fd });
    const ms = Math.round(performance.now() - t0);
    const text = await resp.text();

    if (!resp.ok) {
      statusEl.textContent = '失败：HTTP ' + resp.status;
      document.getElementById('result').innerHTML =
        '<div class="verdict unk">请求失败</div><div class="card"><pre>' +
        text.replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c])) +
        '</pre></div>';
      return;
    }
    statusEl.textContent = '完成，用时 ' + ms + ' ms';
    render(JSON.parse(text));
  } catch (e) {
    statusEl.textContent = '出错：' + e.message;
  }
}

function kv(label, val) {
  return '<tr><td>' + label + '</td><td>' + val + '</td></tr>';
}

function render(d) {
  let cls, title;
  if (d.upright === true)       { cls = 'ok';  title = '端正'; }
  else if (d.upright === false) { cls = 'bad'; title = '不端正'; }
  else                          { cls = 'unk'; title = '无法判定'; }

  const reasonMap = {
    fast_path_ok: '快速通道判定端正',
    fast_path_bad: '快速通道判定不端正',
    no_person: '画面中没有人',
    low_kp_conf: '关键点置信度不足（太暗 / 人太小 / 姿势遮挡）',
    bad_shoulder_width: '双肩重合，无法计算肩宽',
    cannot_determine: '两个通道都无法判定'
  };

  let html = '<div class="verdict ' + cls + '">' + title + '</div>';
  html += '<div class="card"><table>';
  html += kv('头高 head_height', d.head_height === null || d.head_height === undefined
             ? '—' : d.head_height);
  html += kv('肩平差 shoulder_level', d.shoulder_level === null || d.shoulder_level === undefined
             ? '—' : d.shoulder_level);
  html += kv('动作 action', d.action === 'blow' ? 'blow（触发吹风）' : 'none（不吹）');
  html += kv('判定来源 source', d.source || '—');
  html += kv('状态 state', d.state || '—');
  html += kv('原因 reason', reasonMap[d.reason] || d.reason || '—');
  html += '</table>';
  html += '<div class="hint">服务器当前阈值：头高 &lt; 0.15 判低头，肩平差 &gt; 0.05 判侧歪（默认值，待标定）</div>';
  html += '</div>';
  html += '<div class="card"><pre>' +
          JSON.stringify(d, null, 2).replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c])) +
          '</pre></div>';

  const old = document.getElementById('result');
  if (old) old.remove();
  const div = document.createElement('div');
  div.id = 'result';
  div.innerHTML = html;
  statusEl.parentNode.parentNode.after(div);
  div.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

cam.addEventListener('change', e => upload(e.target.files[0]));
lib.addEventListener('change', e => upload(e.target.files[0]));
</script>
</body>
</html>
"""


def error_page(title: str, detail: str) -> bytes:
    return (
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>出错了</title>"
        "<style>body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;"
        "padding:20px;background:#f4f5f7}div{background:#fff;border-radius:12px;padding:18px;"
        "box-shadow:0 1px 3px rgba(0,0,0,.08)}h2{color:#d02b2b;margin:0 0 10px;font-size:18px}"
        "pre{background:#1f2430;color:#d7dce5;padding:12px;border-radius:8px;font-size:12px;"
        "overflow-x:auto}a{display:inline-block;margin-top:14px;color:#1677ff;text-decoration:none}"
        "</style></head><body><div>"
        f"<h2>{title}</h2><pre>{detail}</pre>"
        "<a href='/'>← 返回重试</a></div></body></html>"
    ).encode("utf-8")


def forward_to_cloud(image_bytes: bytes) -> tuple[int, str]:
    """把图片转发到云端 /detect，返回 (状态码, 响应文本)。"""
    req = urllib.request.Request(
        SERVER_DETECT,
        data=image_bytes,
        method="POST",
        headers={
            "X-Device-Key": DEVICE_KEY,
            "Content-Type": "image/jpeg",
            "Content-Length": str(len(image_bytes)),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


class Handler(BaseHTTPRequestHandler):
    server_version = "PostureCapture/1.0"

    def log_message(self, fmt, *args):  # 精简日志
        sys.stderr.write("  [%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/ping":
            self._send(200, b"pong", "text/plain; charset=utf-8")
        else:
            self._send(404, error_page("路径不存在", self.path), "text/html; charset=utf-8")

    def do_POST(self):  # noqa: N802
        if self.path != "/detect":
            self._send(404, error_page("路径不存在", self.path), "text/html; charset=utf-8")
            return

        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            self._send(400, error_page("请求格式不对", "期望 multipart/form-data"), "text/html; charset=utf-8")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_UPLOAD:
            self._send(
                413,
                error_page("文件太大或为空", f"Content-Length = {length}, 上限 {MAX_UPLOAD} 字节"),
                "text/html; charset=utf-8",
            )
            return

        raw = self.rfile.read(length)

        # 用标准库解析 multipart，取出 image 字段
        header = f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
        msg = BytesParser(policy=HTTP).parsebytes(header + raw)
        image_bytes = None
        if msg.is_multipart():
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if name == "image":
                    image_bytes = part.get_payload(decode=True)
                    break

        if not image_bytes:
            self._send(400, error_page("没收到图片", "表单里找不到 image 字段"), "text/html; charset=utf-8")
            return

        code, text = forward_to_cloud(image_bytes)
        sys.stderr.write(f"  -> 云端返回 HTTP {code}  {len(image_bytes)} 字节\n")

        if code == 200:
            self._send(200, text.encode("utf-8"), "application/json; charset=utf-8")
        else:
            detail = text.replace("<", "&lt;").replace(">", "&gt;")
            self._send(
                code or 502,
                error_page(f"云端返回 HTTP {code}", detail),
                "text/html; charset=utf-8",
            )


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def outbound_ip() -> str:
    """取本机对外通信时使用的 IP（手机要访问的就是它）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 53))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        s.close()


def main() -> int:
    port = LISTEN_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"端口参数无效：{sys.argv[1]}", file=sys.stderr)
            return 2

    ip = outbound_ip()
    print("=" * 62)
    print("  坐姿检测 · 手机拍照测试")
    print("=" * 62)
    print(f"  手机浏览器打开：  http://{ip}:{port}")
    print()
    print(f"  转发目标：        {SERVER_DETECT}")
    print(f"  设备密钥：        {DEVICE_KEY[:8]}…（已隐藏后段）")
    print()
    print("  手机需与本机连同一个 WiFi。按 Ctrl+C 停止。")
    print("=" * 62)
    sys.stdout.flush()

    try:
        srv = Server(("0.0.0.0", port), Handler)
    except OSError as e:
        print(f"\n启动失败：{e}", file=sys.stderr)
        print(f"端口 {port} 可能被占用，换一个试试：python capture_server.py 8766", file=sys.stderr)
        return 1

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
