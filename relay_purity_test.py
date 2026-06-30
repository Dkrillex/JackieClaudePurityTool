#!/usr/bin/env python3
"""聚合平台 / API 中转资源纯度测试工具 (Relay Purity Test) v1.0

用一套**客观探针**给任意 API 中转 / 聚合平台的模型资源打"纯度分"，
帮你判断某个渠道的资源是否"纯"，还是被"掺水 / 降智 / 套壳 / 篡改"：

  - 协议是否齐全（原生 Anthropic 协议是否真可用）；
  - 模型是否被降智到老模型；
  - 推理能力是否完整；
  - 用户指令是否被隐藏系统提示覆盖；
  - 流式响应是否被篡改；
  - 错误响应是否泄露密钥 / 上游。

设计原则：
  1. 只用**可验证的客观证据**打分（知识新鲜度、推理陷阱、原生协议可用性等），
     不把"模型自报身份"当作降智证据 —— 它是幻觉，仅作信息记录。
  2. 零第三方依赖：只用 Python 标准库 + 系统 curl，任何机器一键运行。

用法：
  # 1) 测试单个渠道
  python3 relay_purity_test.py --url https://your-relay.com/v1 \
      --key sk-xxx --model claude-opus-4-8 --name 渠道A

  # 2) 多渠道对照（推荐，凸显纯度差异）
  python3 relay_purity_test.py --config providers.json --output report.md

providers.json 示例见同目录 relay_purity_providers.example.json。
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ============================================================
# 传输层：curl（零依赖、可穿透自签名/代理环境）
# ============================================================

@dataclass
class Resp:
    """一次 HTTP 调用的归一化结果。"""
    status: int                 # HTTP 状态码；0 表示传输层失败（超时/连接错误）
    elapsed: float              # 总耗时（秒）
    headers: dict = field(default_factory=dict)   # 响应头（键已小写）
    body: str = ""              # 响应体文本
    error: Optional[str] = None # 传输层错误信息

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


def _cleanup(*paths):
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


# ============================================================
# 中转客户端：OpenAI + Anthropic 双协议
# ============================================================

class RelayClient:
    """统一封装一个中转端点，支持 OpenAI 与 Anthropic 两种协议。"""

    def __init__(self, url: str, key: str, model: str, timeout: int = 60):
        self.root = url.rstrip("/")
        if self.root.endswith("/v1"):
            self.root = self.root[:-3]
        self.key = key
        self.model = model
        self.timeout = timeout
        self._fmt = None  # 缓存可用协议："openai" | "anthropic"

    # -- 端点 ----------------------------------------------------------------
    @property
    def openai_url(self):
        return f"{self.root}/v1/chat/completions"

    @property
    def anthropic_url(self):
        return f"{self.root}/v1/messages"

    @property
    def models_url(self):
        return f"{self.root}/v1/models"

    # -- 协议调用 ------------------------------------------------------------
    def call_openai(self, prompt, system=None, max_tokens=512, stream=False):
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        body = {"model": self.model, "max_tokens": max_tokens, "messages": msgs}
        if stream:
            body["stream"] = True
        headers = {
            "Authorization": f"Bearer {self.key}",
            "content-type": "application/json",
        }
        return curl_request("POST", self.openai_url, headers, body, self.timeout)

    def call_anthropic(self, prompt, system=None, max_tokens=512):
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        headers = {
            "x-api-key": self.key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        return curl_request("POST", self.anthropic_url, headers, body, self.timeout)

    def list_models(self):
        headers = {"Authorization": f"Bearer {self.key}"}
        return curl_request("GET", self.models_url, headers, None, self.timeout)

    # -- 便捷封装 ------------------------------------------------------------
    def smart_ask(self, prompt, system=None, max_tokens=512):
        """优先 OpenAI 协议提问，失败则回退 Anthropic，返回纯文本回复。"""
        order = (["openai", "anthropic"] if self._fmt != "anthropic"
                 else ["anthropic", "openai"])
        last = ""
        for fmt in order:
            if fmt == "openai":
                resp = self.call_openai(prompt, system, max_tokens)
                text = extract_openai_text(resp)
            else:
                resp = self.call_anthropic(prompt, system, max_tokens)
                text = extract_anthropic_text(resp)
            if resp.ok and text:
                self._fmt = fmt
                return text, resp
            last = resp.error or f"HTTP {resp.status}"
        return "", Resp(status=0, elapsed=0.0, error=last)


def extract_openai_text(resp: Resp) -> str:
    data = resp.json()
    if not data:
        return ""
    try:
        choice = data["choices"][0]
        content = choice.get("message", {}).get("content", "")
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, list):  # 部分网关返回分段 content
        return "".join(
            seg.get("text", "") for seg in content if isinstance(seg, dict)
        )
    return content or ""


def extract_anthropic_text(resp: Resp) -> str:
    data = resp.json()
    if not data:
        return ""
    content = data.get("content")
    if isinstance(content, list):
        return "".join(
            seg.get("text", "") for seg in content
            if isinstance(seg, dict) and seg.get("type") == "text"
        )
    if isinstance(content, str):
        return content
    return ""


# ============================================================
# 文本匹配工具
# ============================================================

def _norm(text: str) -> str:
    return (text or "").lower().strip()


def contains_any(text: str, needles) -> bool:
    t = _norm(text)
    return any(n.lower() in t for n in needles)


# ============================================================
# 探针结果数据结构
# ============================================================

@dataclass
class DimResult:
    """一个纯度维度的评分结果。"""
    key: str
    title: str
    score: float
    maximum: float
    verdict: str            # ok | warn | fail | skip
    details: list = field(default_factory=list)


VERDICT_ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌", "skip": "➖"}


# ============================================================
# 纯度探针（每个返回一个 DimResult）
# ============================================================

def probe_protocol(client: RelayClient) -> DimResult:
    """协议纯度（25 分）：原生 Anthropic /v1/messages + OpenAI /v1/chat/completions。

    很多"套壳"中转只做 OpenAI 格式转换，根本没有实现原生 Anthropic 协议，
    走原生协议的客户端（Claude Code / 官方 SDK）会卡死。
    """
    details, score = [], 0.0

    # OpenAI 格式
    o = client.call_openai("ping", max_tokens=5)
    if o.ok and extract_openai_text(o):
        score += 10
        details.append(f"OpenAI `/v1/chat/completions` 可用（{o.elapsed:.2f}s）")
    else:
        details.append(
            f"OpenAI `/v1/chat/completions` **不可用**（HTTP {o.status}"
            f"{'/' + o.error if o.error else ''}）"
        )

    # Anthropic 原生格式
    a = client.call_anthropic("ping", max_tokens=5)
    if a.ok:
        data = a.json() or {}
        rid = str(data.get("id", ""))
        if rid.startswith("msg_") and extract_anthropic_text(a):
            score += 15
            details.append(
                f"原生 Anthropic `/v1/messages` 可用，响应 id=`{rid[:16]}…`"
                f"（真原生，{a.elapsed:.2f}s）"
            )
        else:
            score += 7
            details.append(
                "原生 `/v1/messages` 返回 200，但响应 id 非 `msg_` 前缀 —— "
                "疑似 OpenAI 转换层模拟，并非真原生协议"
            )
    else:
        reason = a.error or f"HTTP {a.status}"
        details.append(
            f"原生 Anthropic `/v1/messages` **不可用**（{reason}）—— "
            "走原生协议的客户端（Claude Code/官方 SDK）会卡死"
        )

    verdict = "ok" if score >= 22 else ("warn" if score >= 10 else "fail")
    return DimResult("protocol", "协议纯度", score, 25, verdict, details)


def probe_freshness(client: RelayClient) -> DimResult:
    """模型新鲜度（25 分）：客观知识探针，排除降智到老模型。

    老模型（如 Claude 3.5 Sonnet，训练截止 2024-04）不可能答对这些。
    """
    details, score = [], 0.0

    # 探针 1：2024-11 美国大选（最强判别点，15 分）
    t1, _ = client.smart_ask(
        "2024年11月的美国总统大选最终由谁当选？只回答当选者姓氏，不要解释。",
        max_tokens=32,
    )
    if contains_any(t1, ["trump", "特朗普", "川普"]):
        score += 15
        details.append("知道 2024-11 美国大选结果（Trump）→ 训练数据新于 2024-04 ✓")
    else:
        details.append(
            f"未答对 2024-11 大选（回答：{_clip(t1)}）→ 疑似降智到老模型"
        )

    # 探针 2：GPT-4o 发布年份（2024-05，老模型不知，10 分）
    t2, _ = client.smart_ask(
        "OpenAI 的 GPT-4o 模型是在哪一年首次发布的？只回答 4 位数年份。",
        max_tokens=16,
    )
    if "2024" in t2:
        score += 10
        details.append("知道 GPT-4o 发布于 2024 年 ✓")
    else:
        details.append(f"不知道 GPT-4o 发布年份（回答：{_clip(t2)}）")

    verdict = "ok" if score >= 22 else ("warn" if score >= 12 else "fail")
    return DimResult("freshness", "模型新鲜度", score, 25, verdict, details)


def probe_reasoning(client: RelayClient) -> DimResult:
    """推理完整性（20 分）：经典陷阱题，弱/量化模型常翻车。"""
    details, score = [], 0.0

    # 球棒与球（弱模型常误答 0.10）
    t1, _ = client.smart_ask(
        "一支球棒和一个球总共 1.10 美元，球棒比球贵 1.00 美元。"
        "请问球多少钱？只回答金额。",
        max_tokens=32,
    )
    if contains_any(t1, ["0.05", "5 美分", "5美分", "5 cents", "5 cent", ".05"]):
        score += 10
        details.append("球棒与球陷阱题答对（$0.05）✓")
    else:
        details.append(f"球棒与球陷阱题答错（回答：{_clip(t1)}，弱模型常误答 $0.10）")

    # 兄妹关系（Sally 谜题）
    t2, _ = client.smart_ask(
        "莎莉有 3 个哥哥，每个哥哥都有 2 个妹妹。请问莎莉有几个妹妹？只回答数字。",
        max_tokens=16,
    )
    if re.search(r"(?<!\d)1(?!\d)", t2) and not re.search(r"(?<!\d)[2-9]", t2):
        score += 10
        details.append("兄妹关系推理题答对（1 个妹妹）✓")
    else:
        details.append(f"兄妹关系推理题答错（回答：{_clip(t2)}，正确答案为 1）")

    verdict = "ok" if score >= 18 else ("warn" if score >= 10 else "fail")
    return DimResult("reasoning", "推理完整性", score, 20, verdict, details)


def probe_instruction(client: RelayClient) -> DimResult:
    """指令忠实度（15 分）：检测中转是否注入了覆盖用户的隐藏系统提示。

    若中转私自塞入自己的 system prompt（套壳常见），用户指令会被破坏。
    """
    details, score = [], 0.0

    # 前缀指令
    t1, _ = client.smart_ask(
        "用一句话介绍你自己。",
        system="你必须在每一条回复的最开头原样加上标记 [CHK]，然后再正常回答。",
        max_tokens=64,
    )
    if _norm(t1).startswith("[chk]"):
        score += 8
        details.append("遵守自定义前缀指令 [CHK] ✓")
    else:
        details.append(f"未遵守前缀指令（回复：{_clip(t1)}）→ 疑似隐藏系统提示覆盖")

    # 极简约束指令
    t2, _ = client.smart_ask(
        "今天天气怎么样？",
        system="无论用户问什么，你只能回复两个字符 OK，不得输出任何其他内容。",
        max_tokens=16,
    )
    if re.sub(r"[\s\.\!。！]", "", _norm(t2)) == "ok":
        score += 7
        details.append("遵守极简约束指令（仅回复 OK）✓")
    else:
        details.append(f"未遵守约束指令（回复：{_clip(t2)}）")

    verdict = "ok" if score >= 13 else ("warn" if score >= 7 else "fail")
    return DimResult("instruction", "指令忠实度", score, 15, verdict, details)


def probe_stream(client: RelayClient) -> DimResult:
    """流式完整性（10 分）：SSE 结构是否正常、模型名是否一致、是否有 [DONE]。"""
    details, score = [], 0.0
    resp = client.call_openai("数到 5，用逗号分隔。", max_tokens=64, stream=True)

    if not (resp.ok or resp.status == 200) or "data:" not in resp.body:
        return DimResult(
            "stream", "流式完整性", 0, 10, "fail",
            [f"流式接口不可用或非标准 SSE（HTTP {resp.status}"
             f"{'/' + resp.error if resp.error else ''}）"],
        )

    chunks, models, has_done, has_content = 0, set(), False, False
    for line in resp.body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            has_done = True
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        chunks += 1
        if obj.get("model"):
            models.add(obj["model"])
        try:
            delta = obj["choices"][0].get("delta", {})
            if delta.get("content"):
                has_content = True
        except (KeyError, IndexError, TypeError):
            pass

    if chunks >= 1 and has_content:
        score += 4
        details.append(f"收到 {chunks} 个有效 SSE 分片且含内容 ✓")
    else:
        details.append("SSE 分片缺失或无增量内容")
    if has_done:
        score += 2
        details.append("正常 `[DONE]` 结束标记 ✓")
    else:
        details.append("缺少 `[DONE]` 结束标记")
    if len(models) <= 1:
        score += 2
        details.append(
            f"流内模型名一致（{next(iter(models)) if models else 'n/a'}）✓"
        )
    else:
        details.append(f"流内模型名不一致：{sorted(models)} → 疑似中途切换/篡改")
    if chunks >= 2:
        score += 2
        details.append("分片粒度正常（多分片流式）✓")

    verdict = "ok" if score >= 8 else ("warn" if score >= 4 else "fail")
    return DimResult("stream", "流式完整性", score, 10, verdict, details)


def probe_leakage(client: RelayClient) -> DimResult:
    """凭据/错误安全（5 分）：构造畸形请求，看错误响应是否泄露密钥/上游。"""
    details, score = [], 5.0
    headers = {
        "Authorization": f"Bearer {client.key}",
        "content-type": "application/json",
    }
    # 故意发畸形 JSON 触发错误路径
    resp = curl_request("POST", client.openai_url, headers,
                        '{"model": , broken json', client.timeout)
    if resp.status == 0:
        return DimResult(
            "leakage", "凭据/错误安全", 0, 5, "skip",
            [f"端点无法触达，跳过（{resp.error or '传输失败'}）"],
        )
    blob = (resp.body or "") + " " + " ".join(
        f"{k}:{v}" for k, v in resp.headers.items()
    )
    low = blob.lower()

    if client.key and client.key in blob:
        score = 0
        details.append("**严重**：错误响应中回显了完整 API 密钥（凭据直接泄露）")
    elif re.search(r"sk-[a-z0-9]{20,}", low):
        score = min(score, 1)
        details.append("**高危**：错误响应中疑似泄露 API 密钥片段")
    if re.search(r"(upstream|proxy_pass|127\.0\.0\.1|localhost:\d|:3000|/var/|traceback|panic:)", low):
        score = min(score, 2)
        details.append("错误响应泄露上游地址/路径/堆栈等内部信息")

    if score == 5:
        details.append(f"畸形请求错误响应未泄露敏感信息（HTTP {resp.status}）✓")

    verdict = "ok" if score >= 5 else ("warn" if score >= 2 else "fail")
    return DimResult("leakage", "凭据/错误安全", score, 5, verdict, details)


def _clip(text: str, n: int = 60) -> str:
    text = (text or "").replace("\n", " ").strip()
    return (text[:n] + "…") if len(text) > n else (text or "(空)")


PROBES = [
    probe_protocol,
    probe_freshness,
    probe_reasoning,
    probe_instruction,
    probe_stream,
    probe_leakage,
]


# ============================================================
# 渠道评估
# ============================================================

@dataclass
class ProviderResult:
    name: str
    url: str
    model: str
    reference: bool
    dims: list = field(default_factory=list)
    self_report: str = ""   # 模型自报身份（仅信息记录，不计分）

    @property
    def total(self) -> float:
        return round(sum(d.score for d in self.dims), 1)

    @property
    def grade(self) -> str:
        t = self.total
        if t >= 90:
            return "纯净 PURE ✅"
        if t >= 70:
            return "基本纯净 ☑️"
        if t >= 45:
            return "掺水 ⚠️"
        return "严重掺假 ❌"


def evaluate(client: RelayClient, name: str, reference: bool) -> ProviderResult:
    print(f"\n  ── 测试渠道：{name}（{client.model} @ {client.root}）")
    dims = []
    for probe in PROBES:
        d = probe(client)
        icon = VERDICT_ICON.get(d.verdict, "")
        print(f"     {icon} {d.title:<10} {d.score:>4.1f}/{d.maximum:<4.0f}")
        dims.append(d)

    # 模型自报身份：仅信息记录（方法论：不可靠，不计分）
    self_report, _ = client.smart_ask(
        "你是哪个模型？请只回答模型名称与版本。", max_tokens=48
    )
    res = ProviderResult(name, client.root, client.model, reference, dims,
                         _clip(self_report, 80))
    print(f"     ── 纯度总分：{res.total}/100 → {res.grade}")
    return res


# ============================================================
# 报告渲染
# ============================================================

def render_markdown(results: list) -> str:
    out = []
    out.append("# 聚合平台 / 中转资源纯度对照报告")
    out.append("")
    out.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    out.append("- 工具：Relay Purity Test v1.0（零依赖 · curl 直连 · 客观探针）")
    out.append("- 评分维度：协议纯度(25) · 模型新鲜度(25) · 推理完整性(20) · "
               "指令忠实度(15) · 流式完整性(10) · 凭据安全(5)")
    out.append("")

    # 一句话结论
    ref = next((r for r in results if r.reference), None)
    if ref and len(results) > 1:
        others = [r for r in results if not r.reference]
        worst = min(others, key=lambda r: r.total) if others else None
        out.append("## 一句话结论")
        out.append("")
        line = (f"**{ref.name}** 纯度 **{ref.total}/100（{ref.grade}）**")
        if worst:
            line += (f"，对照组最低为 **{worst.name} {worst.total}/100"
                     f"（{worst.grade}）**")
        out.append(line + "。")
        out.append("")

    # 对照总表
    out.append("## 一、纯度总览")
    out.append("")
    header = ("| 渠道 | 协议 | 新鲜度 | 推理 | 指令 | 流式 | 安全 | "
              "**总分** | 评级 |")
    out.append(header)
    out.append("|------|------|--------|------|------|------|------|------|------|")
    for r in sorted(results, key=lambda x: -x.total):
        cells = {d.key: d for d in r.dims}
        star = "⭐ " if r.reference else ""

        def cell(k):
            d = cells.get(k)
            return f"{d.score:.0f}" if d else "-"

        out.append(
            f"| {star}**{r.name}** | {cell('protocol')} | {cell('freshness')} | "
            f"{cell('reasoning')} | {cell('instruction')} | {cell('stream')} | "
            f"{cell('leakage')} | **{r.total}** | {r.grade} |"
        )
    out.append("")
    out.append("> ⭐ = 基准渠道。分数越高代表资源越"
               "\"纯\"（原生协议齐全、模型不降智、无篡改）。")
    out.append("")

    # 逐家明细
    out.append("## 二、逐渠道明细")
    for r in sorted(results, key=lambda x: -x.total):
        out.append("")
        out.append(f"### {r.name} — {r.total}/100（{r.grade}）")
        out.append("")
        out.append(f"- 端点：`{r.url}`　模型：`{r.model}`")
        out.append("")
        for d in r.dims:
            icon = VERDICT_ICON.get(d.verdict, "")
            out.append(f"**{icon} {d.title}　{d.score:.1f}/{d.maximum:.0f}**")
            out.append("")
            for detail in d.details:
                out.append(f"- {detail}")
            out.append("")
        out.append(f"> 模型自报身份（仅信息记录，不计分）：{r.self_report or '(无)'}")

    # 方法论声明
    out.append("")
    out.append("## 三、方法论与局限")
    out.append("")
    out.append("1. 本工具只用**可客观验证的证据**打分（原生协议可用性、"
               "知识新鲜度、推理陷阱、指令忠实度、流式结构、错误泄露）。")
    out.append("2. **模型自报身份不计分**：新模型语料含大量旧模型自述，"
               "自报常为幻觉，不能作为降智依据，仅作信息记录。")
    out.append("3. 黑盒手段无法 100% 确证确切版本号，只能给出"
               "\"纯度档位\"；建议结合后台计费单价核对。")
    return "\n".join(out)


def print_summary(results: list):
    print("\n" + "=" * 64)
    print("  纯度对照总览（分数越高资源越纯）")
    print("=" * 64)
    name_w = max((len(r.name) for r in results), default=8) + 2
    print(f"  {'渠道':<{name_w}} {'总分':>6}  评级")
    print("  " + "-" * (name_w + 24))
    for r in sorted(results, key=lambda x: -x.total):
        star = "⭐" if r.reference else " "
        print(f"  {star}{r.name:<{name_w}} {r.total:>6.1f}  {r.grade}")
    print("=" * 64 + "\n")


# ============================================================
# CLI
# ============================================================

def load_providers(args):
    """从 --config 或单组 --url/--key/--model 构造待测渠道列表。"""
    providers = []
    if args.config:
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        items = cfg.get("providers", cfg) if isinstance(cfg, dict) else cfg
        for it in items:
            providers.append({
                "name": it.get("name", it["url"]),
                "url": it["url"],
                "key": it["key"],
                "model": it.get("model", "claude-opus-4-8"),
                "reference": bool(it.get("reference", False)),
            })
    elif args.url and args.key:
        name = args.name or args.url
        providers.append({
            "name": name, "url": args.url, "key": args.key,
            "model": args.model, "reference": args.reference,
        })
    return providers


def parse_args():
    p = argparse.ArgumentParser(
        description="聚合平台 / API 中转资源纯度测试工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", help="多渠道配置 JSON 文件路径")
    p.add_argument("--url", help="单渠道 Base URL，如 https://your-relay.com/v1")
    p.add_argument("--key", help="单渠道 API Key")
    p.add_argument("--model", default="claude-opus-4-8", help="模型名")
    p.add_argument("--name", help="单渠道显示名")
    p.add_argument("--reference", action="store_true",
                   help="将该渠道标记为对照基准")
    p.add_argument("--timeout", type=int, default=60, help="单请求超时（秒）")
    p.add_argument("--output", help="Markdown 报告输出路径")
    return p.parse_args()


def main():
    args = parse_args()
    providers = load_providers(args)
    if not providers:
        print("错误：请用 --config 提供配置，或用 --url/--key 指定单个渠道。",
              file=sys.stderr)
        print("示例：python3 relay_purity_test.py --url https://your-relay.com/v1 "
              "--key sk-xxx --model claude-opus-4-8 --name 渠道A", file=sys.stderr)
        return 2

    print("\n" + "=" * 64)
    print("  Relay Purity Test 资源纯度测试 v1.0")
    print(f"  待测渠道：{len(providers)} 个")
    print("=" * 64)

    results = []
    for prov in providers:
        client = RelayClient(prov["url"], prov["key"], prov["model"], args.timeout)
        results.append(evaluate(client, prov["name"], prov["reference"]))

    print_summary(results)

    md = render_markdown(results)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"  Markdown 报告已保存：{args.output}\n")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
