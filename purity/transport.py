"""传输层：基于系统 curl 的零依赖 HTTP 客户端。

使用 curl 而非 urllib/requests 的原因：
  - 零第三方依赖，任何装了 curl 的机器都能跑；
  - 天然支持自签名证书（-k）、绕过系统代理（--noproxy *）等中转常见环境。
"""

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
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


def curl_request(method: str, url: str, headers: dict, body=None,
                 timeout: int = 60) -> Resp:
    """通过 curl 发送请求并返回归一化的 Resp。

    使用 -D/-o 分离头与体，-w 取状态码与总耗时，避免解析 -i 混排输出。
    """
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
        _cleanup(hdr_f.name, body_f.name)
        return Resp(status=0, elapsed=0.0, error="curl-not-found")

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
