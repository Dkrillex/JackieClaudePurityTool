#!/usr/bin/env python3
"""聚合平台 / 中转资源纯度测试 —— Streamlit 可视化界面

在网页上配置待测渠道、勾选探针、一键运行，实时查看纯度对照评分，
并导出可下载的 Markdown 报告。

运行：
  pip install -r requirements.txt
  streamlit run app.py
"""

import os
import sys
from datetime import datetime

import pandas as pd
import streamlit as st

# 复用同仓库的核心探测逻辑（relay_purity_test.py 在上一级目录）
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

# 直接从核心模块 relay_purity_test.py 导入探测能力（这就是本界面的"引擎"）
from relay_purity_test import (  # noqa: E402
    RelayClient,            # 渠道客户端（OpenAI + Anthropic 双协议）
    ProviderResult,         # 单渠道评估结果
    VERDICT_ICON,           # 判定图标
    render_markdown,        # 生成 Markdown 报告
    _clip,                  # 文本截断工具
    probe_protocol,         # 协议纯度探针
    probe_freshness,        # 模型新鲜度探针
    probe_reasoning,        # 推理完整性探针
    probe_instruction,      # 指令忠实度探针
    probe_stream,           # 流式完整性探针
    probe_leakage,          # 凭据/错误安全探针
)

# 探针：界面标题 -> 核心模块里的探针函数（左侧勾选用）
PROBE_FUNCS = {
    "协议纯度 (25)": probe_protocol,
    "模型新鲜度 (25)": probe_freshness,
    "推理完整性 (20)": probe_reasoning,
    "指令忠实度 (15)": probe_instruction,
    "流式完整性 (10)": probe_stream,
    "凭据/错误安全 (5)": probe_leakage,
}


st.set_page_config(
    page_title="资源纯度测试",
    page_icon="🧪",
    layout="wide",
)


# ============================================================
# 运行逻辑
# ============================================================

def run_provider(prov: dict, probe_fns: list, timeout: int,
                 fetch_self_report: bool) -> ProviderResult:
    """对单个渠道运行选定探针，返回 ProviderResult。"""
    client = RelayClient(prov["url"], prov["key"], prov["model"], timeout)
    dims = [fn(client) for fn in probe_fns]
    self_report = ""
    if fetch_self_report:
        sr, _ = client.smart_ask(
            "你是哪个模型？请只回答模型名称与版本。", max_tokens=48
        )
        self_report = _clip(sr, 80)
    return ProviderResult(
        prov["name"], client.root, prov["model"],
        bool(prov.get("reference")), dims, self_report,
    )


def results_to_dataframe(results: list) -> pd.DataFrame:
    rows = []
    for r in sorted(results, key=lambda x: -x.total):
        cells = {d.key: d.score for d in r.dims}
        rows.append({
            "基准": "⭐" if r.reference else "",
            "渠道": r.name,
            "模型": r.model,
            "协议": cells.get("protocol", None),
            "新鲜度": cells.get("freshness", None),
            "推理": cells.get("reasoning", None),
            "指令": cells.get("instruction", None),
            "流式": cells.get("stream", None),
            "安全": cells.get("leakage", None),
            "总分": r.total,
            "评级": r.grade,
        })
    return pd.DataFrame(rows)


# ============================================================
# 侧边栏：全局设置
# ============================================================

st.sidebar.title("⚙️ 测试设置")
timeout = st.sidebar.slider("单请求超时（秒）", 10, 180, 60, 5)
default_model = st.sidebar.text_input("默认模型名", value="claude-opus-4-8")
fetch_self_report = st.sidebar.checkbox("采集模型自报身份（仅记录，不计分）", value=True)

st.sidebar.markdown("---")
st.sidebar.subheader("勾选要运行的探针")
selected_titles = []
for title in PROBE_FUNCS:
    if st.sidebar.checkbox(title, value=True, key=f"probe_{title}"):
        selected_titles.append(title)
selected_fns = [PROBE_FUNCS[t] for t in selected_titles]

st.sidebar.markdown("---")
st.sidebar.caption(
    "提示：分越高代表资源越「纯」。满分 100 需勾选全部探针；"
    "只跑部分探针时总分上限会相应降低。"
)


# ============================================================
# 主区域：标题 + 配置
# ============================================================

st.title("🧪 聚合平台 / 中转资源纯度测试")
st.caption(
    "客观探针 · 协议纯度 / 模型新鲜度 / 推理 / 指令忠实 / 流式 / 凭据安全　"
    "—— 一键评估某渠道资源是否被「掺水 / 降智 / 套壳 / 篡改」"
)

st.subheader("1) 配置待测渠道")
st.caption("可直接编辑表格；勾选「基准」把某渠道作为对照参照（建议是你信任的纯净源）。")

# 可上传 JSON 配置导入
up = st.file_uploader("（可选）导入 providers.json 配置", type=["json"])
default_rows = pd.DataFrame([
    {"name": "渠道A", "url": "https://your-relay.com/v1", "key": "",
     "model": default_model, "reference": True},
])
if up is not None:
    try:
        import json
        cfg = json.load(up)
        items = cfg.get("providers", cfg) if isinstance(cfg, dict) else cfg
        default_rows = pd.DataFrame([
            {
                "name": it.get("name", it.get("url", "")),
                "url": it.get("url", ""),
                "key": it.get("key", ""),
                "model": it.get("model", default_model),
                "reference": bool(it.get("reference", False)),
            }
            for it in items
        ])
        st.success(f"已导入 {len(default_rows)} 个渠道")
    except Exception as e:  # noqa: BLE001
        st.error(f"配置解析失败：{e}")

edited = st.data_editor(
    default_rows,
    num_rows="dynamic",
    use_container_width=True,
    column_config={
        "name": st.column_config.TextColumn("渠道名", width="medium"),
        "url": st.column_config.TextColumn("Base URL", width="large"),
        "key": st.column_config.TextColumn("API Key", width="large"),
        "model": st.column_config.TextColumn("模型", width="medium"),
        "reference": st.column_config.CheckboxColumn("基准", width="small"),
    },
    key="providers_editor",
)

run = st.button("🚀 开始测试", type="primary", use_container_width=True)


# ============================================================
# 运行 + 结果展示
# ============================================================

if run:
    providers = [
        r for r in edited.to_dict("records")
        if str(r.get("url", "")).strip() and str(r.get("key", "")).strip()
    ]
    if not providers:
        st.warning("请至少填写一个含 URL 和 Key 的渠道。")
        st.stop()
    if not selected_fns:
        st.warning("请在左侧至少勾选一个探针。")
        st.stop()

    progress = st.progress(0.0, text="准备测试…")
    results = []
    total = len(providers)
    for i, prov in enumerate(providers):
        progress.progress(
            i / total, text=f"正在测试：{prov.get('name') or prov['url']}（{i+1}/{total}）"
        )
        try:
            res = run_provider(prov, selected_fns, timeout, fetch_self_report)
        except Exception as e:  # noqa: BLE001
            st.error(f"渠道 {prov.get('name')} 测试出错：{e}")
            continue
        results.append(res)
    progress.progress(1.0, text="测试完成 ✓")

    if not results:
        st.stop()

    st.session_state["results"] = results


# 从 session 读取结果（避免按钮交互后丢失）
results = st.session_state.get("results")
if results:
    st.markdown("---")
    st.subheader("2) 纯度对照总览")

    # 概览指标
    ref = next((r for r in results if r.reference), None)
    best = max(results, key=lambda r: r.total)
    worst = min(results, key=lambda r: r.total)
    c1, c2, c3 = st.columns(3)
    c1.metric("渠道数", len(results))
    c2.metric("最高纯度", f"{best.total:.0f}", best.name)
    c3.metric("最低纯度", f"{worst.total:.0f}", worst.name)

    df = results_to_dataframe(results)

    def _color_total(val):
        try:
            v = float(val)
        except (TypeError, ValueError):
            return ""
        if v >= 90:
            return "background-color:#1b5e20;color:white"
        if v >= 70:
            return "background-color:#33691e;color:white"
        if v >= 45:
            return "background-color:#f9a825;color:black"
        return "background-color:#b71c1c;color:white"

    styled = df.style.map(_color_total, subset=["总分"])
    st.dataframe(styled, use_container_width=True, hide_index=True)
    st.caption("⭐ = 基准渠道。分越高资源越「纯」（原生协议齐全、模型不降智、无篡改）。")

    # 逐渠道明细
    st.subheader("3) 逐渠道明细")
    for r in sorted(results, key=lambda x: -x.total):
        star = "⭐ " if r.reference else ""
        with st.expander(f"{star}{r.name} — {r.total:.0f}/100　{r.grade}",
                         expanded=r.reference):
            st.caption(f"端点：{r.url}　模型：{r.model}")
            for d in r.dims:
                icon = VERDICT_ICON.get(d.verdict, "")
                st.markdown(f"**{icon} {d.title}　{d.score:.1f}/{d.maximum:.0f}**")
                for detail in d.details:
                    st.markdown(f"- {detail}")
            if r.self_report:
                st.caption(f"模型自报身份（仅记录，不计分）：{r.self_report}")

    # 导出报告
    st.subheader("4) 导出报告")
    md = render_markdown(results)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    cda, cdb = st.columns(2)
    cda.download_button(
        "⬇️ 下载 Markdown 报告",
        data=md,
        file_name=f"purity_report_{ts}.md",
        mime="text/markdown",
        use_container_width=True,
    )
    cdb.download_button(
        "⬇️ 下载 CSV 评分表",
        data=df.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"purity_scores_{ts}.csv",
        mime="text/csv",
        use_container_width=True,
    )
    with st.expander("预览 Markdown 报告"):
        st.markdown(md)
