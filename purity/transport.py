"""传输层：HTTP 请求的统一封装，支持两种后端。

  - curl   ：调用系统 `curl`，天然支持自签名证书、绕过系统代理，适合本地 / 服务器；
  - urllib ：纯 Python 标准库，无需任何外部二进制，适合 Serverless（如 Vercel）等
             没有 curl、不允许 subprocess 的沙箱环境。

后端选择由环境变量 `PURITY_TRANSPORT` 控制：
  - "auto"（默认）：本机有 curl 用 curl，否则自动回退 urllib；
  - "curl"        ：强制 curl；
  - "urllib"      ：强制 urllib。

两种后端都返回同一个归一化的 `Resp`，对上层（client / probes）完全透明。
"""

import json
import os
import re
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from shutil import which
from typing import Optional


@dataclass
class Resp:
    """一次 HTTP 调用的归一化结果。"""
    status: int                 # HTTP 状态码；0 表示传输层失败（超时/连接错误）
    elapsed: float              # 总耗时（秒）
    headers: dict = field(default_factory=dict)   # 响应头（键已小写）
    body: str = ""              # 响应体文本
    error: Optional[str] = None  # 传输层错误信息

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self):
        try:
            return json.loads(self.body)
        except Exception:
            return None


# ============================================================
# 公共调度入口
# ============================================================

def curl_request(method: str, url: str, headers: dict, body=None,
                 timeout: int = 60) -> Resp:
    """发送一次 HTTP 请求（函数名保留 curl_request 以兼容旧调用）。

    根据 PURITY_TRANSPORT 选择 curl 或 urllib 后端，默认自动探测。
    """
    mode = os.environ.get("PURITY_TRANSPORT", "auto").lower()
    if mode == "urllib":
        return _urllib_request(method, url, headers, body, timeout)
    if mode == "curl":
        return _curl_request(method, url, headers, body, timeout)
    # auto
    if which("curl"):
        return _curl_request(method, url, headers, body, timeout)
    return _urllib_request(method, url, headers, body, timeout)


# ============================================================
# 后端 1：curl
# ============================================================

def _parse_header_dump(text: str) -> dict:
    """把 curl -D 导出的响应头解析为小写键字典（取最后一段，跳过重定向头）。"""
    blocks = re.split(r"\r?\n\r?\n", text.strip())
    headers: dict = {}
    for block in blocks:
        for line in block.splitlines():
            if ":" in line and not line.upper().startswith("HTTP/"):
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
    return headers


def _cleanup(*paths):
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


def _curl_request(method: str, url: str, headers: dict, body=None,
                  timeout: int = 60) -> Resp:
    """curl 后端：用 -D/-o 分离头与体，-w 取状态码与总耗时。"""
    hdr_f = tempfile.NamedTemporaryFile(delete=False, suffix=".hdr")
    body_f = tempfile.NamedTemporaryFile(delete=False, suffix=".body")
    hdr_f.close()
    body_f.close()

    cmd = [
        "curl", "-sS", "-k", "--noproxy", "*",
        "-X", method, url,
        "-D", hdr_f.name, "-o", body_f.name,
        "-w", "%{http_code} %{time_total}",
        "-m", str(timeout),
    ]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    if body is not None:
        payload = body if isinstance(body, (str, bytes)) else json.dumps(body)
        cmd += ["--data-binary", payload]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout + 15)
    except subprocess.TimeoutExpired:
        _cleanup(hdr_f.name, body_f.name)
        return Resp(status=0, elapsed=float(timeout), error="timeout")
    except FileNotFoundError:
        # 没有 curl：回退到 urllib，保证可用性
        _cleanup(hdr_f.name, body_f.name)
        return _urllib_request(method, url, headers, body, timeout)

    if proc.returncode != 0:
        _cleanup(hdr_f.name, body_f.name)
        return Resp(status=0, elapsed=float(timeout),
                    error=(proc.stderr or "curl-failed").strip()[:200])

    status, _, time_total = proc.stdout.strip().partition(" ")
    try:
        status_i = int(status)
    except ValueError:
        status_i = 0
    try:
        elapsed = float(time_total)
    except ValueError:
        elapsed = 0.0

    with open(hdr_f.name, "r", encoding="utf-8", errors="replace") as f:
        hdr_text = f.read()
    with open(body_f.name, "r", encoding="utf-8", errors="replace") as f:
        body_text = f.read()
    _cleanup(hdr_f.name, body_f.name)

    return Resp(status=status_i, elapsed=elapsed,
                headers=_parse_header_dump(hdr_text), body=body_text)


# ============================================================
# 后端 2：urllib（纯标准库，无外部依赖）
# ============================================================

def _urllib_request(method: str, url: str, headers: dict, body=None,
                    timeout: int = 60) -> Resp:
    """urllib 后端：禁用代理、跳过证书校验（对齐 curl -k）。"""
    data = None
    if body is not None:
        if isinstance(body, bytes):
            data = body
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),          # 绕过系统代理
        urllib.request.HTTPSHandler(context=ctx),  # 跳过证书校验
    )

    start = time.time()
    try:
        resp = opener.open(req, timeout=timeout)
        raw = resp.read()
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
        return Resp(status=getattr(resp, "status", resp.getcode()),
                    elapsed=time.time() - start, headers=hdrs,
                    body=raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""
        hdrs = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
        return Resp(status=e.code, elapsed=time.time() - start, headers=hdrs,
                    body=raw.decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001  超时/连接错误等统一归一化
        return Resp(status=0, elapsed=time.time() - start,
                    error=str(e)[:200] or "transport-error")
