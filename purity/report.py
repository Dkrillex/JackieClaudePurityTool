"""报告渲染：把评估结果输出为 Markdown 报告与控制台总览。"""

from datetime import datetime

from .probes import VERDICT_ICON


def render_markdown(results: list) -> str:
    """把一组 ProviderResult 渲染为 Markdown 对照报告。"""
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
    """控制台打印纯度对照总览。"""
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
