"""纯度探针：每个探针返回一个 DimResult（维度评分）。

满分 100 = 协议纯度(25) + 模型新鲜度(25) + 推理完整性(20)
           + 指令忠实度(15) + 流式完整性(10) + 凭据/错误安全(5)
"""

import re
import json
from dataclasses import dataclass, field

from .client import RelayClient, extract_openai_text, extract_anthropic_text
from .transport import curl_request
from .util import norm, contains_any, clip


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
            f"未答对 2024-11 大选（回答：{clip(t1)}）→ 疑似降智到老模型"
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
        details.append(f"不知道 GPT-4o 发布年份（回答：{clip(t2)}）")

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
        details.append(f"球棒与球陷阱题答错（回答：{clip(t1)}，弱模型常误答 $0.10）")

    # 兄妹关系（Sally 谜题）
    t2, _ = client.smart_ask(
        "莎莉有 3 个哥哥，每个哥哥都有 2 个妹妹。请问莎莉有几个妹妹？只回答数字。",
        max_tokens=16,
    )
    if re.search(r"(?<!\d)1(?!\d)", t2) and not re.search(r"(?<!\d)[2-9]", t2):
        score += 10
        details.append("兄妹关系推理题答对（1 个妹妹）✓")
    else:
        details.append(f"兄妹关系推理题答错（回答：{clip(t2)}，正确答案为 1）")

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
    if norm(t1).startswith("[chk]"):
        score += 8
        details.append("遵守自定义前缀指令 [CHK] ✓")
    else:
        details.append(f"未遵守前缀指令（回复：{clip(t1)}）→ 疑似隐藏系统提示覆盖")

    # 极简约束指令
    t2, _ = client.smart_ask(
        "今天天气怎么样？",
        system="无论用户问什么，你只能回复两个字符 OK，不得输出任何其他内容。",
        max_tokens=16,
    )
    if re.sub(r"[\s\.\!。！]", "", norm(t2)) == "ok":
        score += 7
        details.append("遵守极简约束指令（仅回复 OK）✓")
    else:
        details.append(f"未遵守约束指令（回复：{clip(t2)}）")

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


# 全部探针（顺序即报告展示顺序）
PROBES = [
    probe_protocol,
    probe_freshness,
    probe_reasoning,
    probe_instruction,
    probe_stream,
    probe_leakage,
]
