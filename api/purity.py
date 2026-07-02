"""Vercel Serverless 函数：运行纯度评估并返回 JSON。

POST /api/purity
  请求体 JSON：
    {
      "providers": [
        {"name": "渠道A", "url": "https://...", "key": "sk-...",
         "model": "claude-opus-4-8", "reference": true}
      ],
      "probes": ["protocol","freshness","reasoning","instruction","injection","stream","leakage"],
      "timeout": 30,
      "fetch_self_report": true
    }
  响应体 JSON：
    {"results": [...], "markdown": "..."}

注意：Serverless 沙箱无 curl，传输层自动走纯 Python urllib（见 purity/transport.py）。
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler

# 让函数能 import 到仓库根目录的 purity 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Serverless 无 curl，强制使用纯 Python 传输
os.environ.setdefault("PURITY_TRANSPORT", "urllib")

from purity import (  # noqa: E402
    RelayClient,
    render_markdown,
    probe_protocol,
    probe_freshness,
    probe_reasoning,
    probe_instruction,
    probe_injection,
    probe_stream,
    probe_leakage,
)
from purity.evaluator import evaluate  # noqa: E402

PROBE_BY_KEY = {
    "protocol": probe_protocol,
    "freshness": probe_freshness,
    "reasoning": probe_reasoning,
    "instruction": probe_instruction,
    "injection": probe_injection,
    "stream": probe_stream,
    "leakage": probe_leakage,
}
DEFAULT_KEYS = list(PROBE_BY_KEY.keys())

# 时间预算（秒）：应显著小于 vercel.json 的 maxDuration，给最后一个探针留出收尾余量。
# 可用环境变量 PURITY_BUDGET 覆盖。
TIME_BUDGET = float(os.environ.get("PURITY_BUDGET", "45"))
# 单个上游请求的最长等待（秒），服务端强制封顶。
MAX_REQ_TIMEOUT = int(os.environ.get("PURITY_MAX_REQ_TIMEOUT", "12"))


def _dim_to_dict(d) -> dict:
    return {
        "key": d.key,
        "title": d.title,
        "score": d.score,
        "maximum": d.maximum,
        "verdict": d.verdict,
        "details": d.details,
    }


def _result_to_dict(r) -> dict:
    return {
        "name": r.name,
        "url": r.url,
        "model": r.model,
        "reference": r.reference,
        "total": r.total,
        "max_total": r.max_total,
        "pct": r.pct,
        "grade": r.grade,
        "self_report": r.self_report,
        "dims": [_dim_to_dict(d) for d in r.dims],
    }


def run_audit(payload: dict) -> dict:
    providers = payload.get("providers") or []
    # 单请求超时在服务端封顶，避免某个卡死的上游吃掉整个函数时间预算
    timeout = min(int(payload.get("timeout", 20)), MAX_REQ_TIMEOUT)
    fetch_self_report = bool(payload.get("fetch_self_report", True))
    keys = payload.get("probes") or DEFAULT_KEYS
    probe_fns = [PROBE_BY_KEY[k] for k in keys if k in PROBE_BY_KEY]

    # 时间预算：留出安全余量，赶在 Vercel maxDuration 之前主动收尾并返回 JSON
    budget = float(payload.get("budget", TIME_BUDGET))
    deadline = time.monotonic() + budget
    partial = False

    results = []
    requested = len(probe_fns)
    for prov in providers:
        url = (prov.get("url") or "").strip()
        key = (prov.get("key") or "").strip()
        if not url or not key:
            continue
        if time.monotonic() >= deadline:
            partial = True
            break
        client = RelayClient(url, key, prov.get("model", "claude-opus-4-8"),
                             timeout)
        res = evaluate(
            client,
            name=prov.get("name") or url,
            reference=bool(prov.get("reference")),
            probes=probe_fns,
            fetch_self_report=fetch_self_report,
            deadline=deadline,
        )
        if len(res.dims) < requested:
            partial = True
        results.append(res)

    out = {
        "results": [_result_to_dict(r) for r in results],
        "markdown": render_markdown(results) if results else "",
    }
    if partial:
        out["warning"] = (
            f"已接近 {int(budget)}s 时间预算，仅返回部分结果。"
            "请一次只测 1 个渠道、少勾几个探针、调小超时，或升级 Vercel 计划提高 maxDuration。"
        )
    return out


class handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        self._send(200, {"ok": True, "probes": DEFAULT_KEYS,
                         "hint": "POST providers/probes/timeout to run an audit"})

    def do_POST(self):
        try:
            length = int(self.headers.get("content-length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:  # noqa: BLE001
            return self._send(400, {"error": f"invalid JSON: {e}"})

        try:
            out = run_audit(payload)
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)[:300]})
        return self._send(200, out)
