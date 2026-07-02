"""渠道评估：把多个探针的结果汇总成一个渠道的纯度总分。"""

import time
from dataclasses import dataclass, field

from .client import RelayClient
from .probes import PROBES, VERDICT_ICON
from .util import clip


@dataclass
class ProviderResult:
    """单个渠道的评估结果。"""
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
    def max_total(self) -> float:
        """本次实际运行探针的满分（子集运行时 < 100）。"""
        return round(sum(d.maximum for d in self.dims), 1)

    @property
    def pct(self) -> float:
        """归一化到百分制（按实际运行的满分折算，保证子集也公平）。"""
        m = self.max_total
        return round(self.total / m * 100, 1) if m else 0.0

    @property
    def grade(self) -> str:
        t = self.pct
        if t >= 90:
            return "纯净 PURE ✅"
        if t >= 70:
            return "基本纯净 ☑️"
        if t >= 45:
            return "掺水 ⚠️"
        return "严重掺假 ❌"


def evaluate(client: RelayClient, name: str, reference: bool,
             probes=None, fetch_self_report: bool = True,
             deadline: float = None) -> ProviderResult:
    """对单个渠道运行探针并返回结果（命令行版本会打印进度）。

    Args:
        client: 已构造的渠道客户端。
        name: 渠道显示名。
        reference: 是否为对照基准。
        probes: 要运行的探针函数列表，默认全部 PROBES。
        fetch_self_report: 是否额外采集模型自报身份（仅记录）。
        deadline: `time.monotonic()` 时间戳；到点后停止运行剩余探针，
                  只返回已完成部分（用于 Serverless 防止被平台超时强杀）。
    """
    probes = probes or PROBES
    print(f"\n  ── 测试渠道：{name}（{client.model} @ {client.root}）")
    dims = []
    for probe in probes:
        if deadline is not None and time.monotonic() >= deadline:
            print("     ⏱ 达到时间预算，跳过剩余探针")
            break
        d = probe(client)
        icon = VERDICT_ICON.get(d.verdict, "")
        print(f"     {icon} {d.title:<10} {d.score:>4.1f}/{d.maximum:<4.0f}")
        dims.append(d)

    self_report = ""
    if fetch_self_report and not (deadline is not None
                                  and time.monotonic() >= deadline):
        # 模型自报身份：仅信息记录（方法论：不可靠，不计分）
        sr, _ = client.smart_ask(
            "你是哪个模型？请只回答模型名称与版本。", max_tokens=48
        )
        self_report = clip(sr, 80)

    res = ProviderResult(name, client.root, client.model, reference, dims,
                         self_report)
    print(f"     ── 纯度总分：{res.total}/{res.max_total:.0f} → {res.grade}")
    return res
