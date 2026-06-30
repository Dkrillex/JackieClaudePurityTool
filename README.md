# Relay Purity Test · 聚合平台 / API 中转资源纯度测试工具

用一套**客观探针**给任意 API 中转 / 聚合平台的模型资源打「纯度分」，帮你快速判断
某个渠道的资源是「纯」的，还是被**掺水 / 降智 / 套壳 / 篡改**过。

> 零第三方依赖（核心库只用 Python 标准库 + 系统 `curl`），命令行与网页两种用法。

---

## 它能测什么

| 维度 | 权重 | 说明 |
|------|------|------|
| 协议纯度 | 25 | 原生 Anthropic `/v1/messages` 是否**真可用**（响应 `msg_` 前缀）+ OpenAI `/v1/chat/completions` 是否可用。很多套壳中转只做 OpenAI 转换，原生协议卡死。 |
| 模型新鲜度 | 25 | 客观知识探针（2024-11 美国大选、GPT-4o 发布年份），排除降智到老模型。 |
| 推理完整性 | 20 | 经典陷阱题（球棒与球、兄妹关系），弱 / 量化模型常翻车。 |
| 指令忠实度 | 15 | 自定义前缀 / 极简约束指令，检测中转是否注入了覆盖用户的隐藏系统提示。 |
| 流式完整性 | 10 | SSE 分片结构、`[DONE]` 标记、流内模型名是否一致（防中途切换 / 篡改）。 |
| 凭据/错误安全 | 5 | 畸形请求触发错误路径，检查是否泄露 API 密钥 / 上游地址。 |

**评级**：`≥90 纯净 ✅ ／ ≥70 基本纯净 ☑️ ／ ≥45 掺水 ⚠️ ／ <45 严重掺假 ❌`

> **方法论**：只用可客观验证的证据打分；模型「自报身份」**不计分**（新模型语料含大量旧模型自述，自报常为幻觉，仅作信息记录）。

---

## 目录结构

核心实现统一收敛到 `purity/` 包；`backup/` 仅存放早期单文件 standalone 版本的存档快照（不参与维护）。

```
JackieClaudePurityTool/
├── purity/                         # 核心库（按职责拆分的包，唯一主版本）
│   ├── __init__.py                 #   公开接口 & 版本号
│   ├── __main__.py                 #   支持 python -m purity
│   ├── transport.py                #   curl 传输层（Resp / curl_request）
│   ├── client.py                   #   RelayClient（OpenAI + Anthropic 双协议）
│   ├── util.py                     #   文本工具（norm / contains_any / clip）
│   ├── probes.py                   #   6 个纯度探针（probe_* / PROBES）
│   ├── evaluator.py                #   渠道评估汇总（ProviderResult / evaluate）
│   ├── report.py                   #   Markdown 报告与控制台总览
│   ├── cli.py                      #   命令行入口
│   └── providers.example.json      #   多渠道配置示例
├── relay_purity_ui/                # Streamlit 可视化界面（基于 purity）
│   ├── app.py
│   ├── requirements.txt
│   └── README.md
├── backup/                         # 早期单文件 standalone 版本的存档（仅供参考）
└── docs/                           # 相关报告 / 文档
```

---

## 快速开始（命令行）

需要本机已安装 `python3` 和 `curl`，无需安装任何 Python 包。

```bash
# 1) 测试单个渠道
python3 -m purity \
    --url https://your-relay.com/v1 \
    --key sk-xxx \
    --model claude-opus-4-8 \
    --name 渠道A

# 2) 多渠道对照（推荐，最能凸显纯度差异）
cp purity/providers.example.json providers.json
# 编辑 providers.json 填入真实 url/key/model（含 "reference": true 的为对照基准）
python3 -m purity --config providers.json --output report.md
```

常用参数：

| 参数 | 说明 |
|------|------|
| `--config` | 多渠道配置 JSON 路径 |
| `--url` / `--key` / `--model` / `--name` | 单渠道直填 |
| `--reference` | 把该渠道标记为对照基准 |
| `--timeout` | 单请求超时（秒，默认 60） |
| `--output` | Markdown 报告输出路径（不填则打印到控制台） |

### 配置文件格式

```json
{
  "providers": [
    {"name": "渠道A", "url": "https://relay-a.example.com/v1", "key": "sk-...", "model": "claude-opus-4-8", "reference": true},
    {"name": "渠道B", "url": "https://relay-b.example.com/v1", "key": "sk-...", "model": "claude-opus-4-8"}
  ]
}
```

---

## 网页界面（Streamlit）

可视化配置渠道、勾选探针、一键测试、彩色对照评分、下载 Markdown / CSV 报告。

```bash
cd relay_purity_ui
pip install -r requirements.txt
streamlit run app.py
```

详见 [`relay_purity_ui/README.md`](relay_purity_ui/README.md)。

---

## 作为库调用

```python
from purity import RelayClient, evaluate, render_markdown

client = RelayClient("https://your-relay.com/v1", "sk-xxx", "claude-opus-4-8")
result = evaluate(client, name="渠道A", reference=True)
print(result.total, result.grade)
print(render_markdown([result]))
```

---

## 安全提醒

- 测试需要**真实 API Key**。请勿把含密钥的 `providers.json` 提交进仓库（已在 `.gitignore` 忽略 `providers.json`，仅保留 `*.example.json`）。
- 测试完成后建议在后台**轮换用过的 Key**。
- 工具依赖系统 `curl` 作为传输层，并默认 `-k`（跳过证书校验）以兼容自签名中转。

---

## 局限

黑盒手段无法 100% 确证确切模型版本号，只能给出「纯度档位」。建议结合中转后台的**计费单价**核对，确认所付价格与实际所得模型档位相符。
