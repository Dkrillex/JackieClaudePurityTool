# 部署到 Vercel

本项目已为 Vercel 准备好「静态前端 + Python Serverless 函数」版本，可直接部署。

```
index.html          → 静态前端（Vercel 自动以 / 提供）
api/purity.py       → Serverless 函数（路径 /api/purity）
purity/             → 核心库（被函数 import，已在 vercel.json includeFiles）
vercel.json         → 函数时长/内存 + 传输后端配置
requirements.txt    → 启用 Python 运行时（核心零依赖）
.vercelignore       → 排除 Streamlit / backup / docs 等不需部署的内容
```

> Streamlit 版界面（`relay_purity_ui/`）**不能**部署到 Vercel（Serverless 无法长驻），它只用于本地。Vercel 上用的是 `index.html` 这套。

## 方式 A：网页一键部署（最简单）

1. 把本仓库推到 GitHub。
2. 打开 [vercel.com](https://vercel.com/) → **Add New… → Project** → 导入该仓库。
3. Framework Preset 选 **Other**（无需改动，根目录即可），点 **Deploy**。
4. 部署完成后访问分配的域名，即可在网页上配置渠道并测试。

## 方式 B：命令行部署

```bash
npm i -g vercel
cd JackieClaudePurityTool
vercel          # 首次：按提示登录并创建项目（预览环境）
vercel --prod   # 部署到生产
```

## 必须知道的限制

1. **函数执行时长**：一次完整评估对每个渠道要顺序发 ~10 次模型请求，可能要几十秒。
   - `vercel.json` 已设 `maxDuration: 60`。**Hobby（免费）计划单函数上限约 60s**，渠道多 / 模型慢时可能超时；
   - 建议：**一次只测 1 个渠道**、或在前端**少勾几个探针**、把超时调小；需要更长时长请用 Pro 计划。
2. **无 curl**：Serverless 沙箱没有 `curl`，已通过 `PURITY_TRANSPORT=urllib`（纯 Python 传输）解决，无需你操作。
3. **冷启动**：首次请求可能多几百毫秒，正常。

## 安全（重要）

- 网页会把填入的 **API Key 明文发到 `/api/purity`** 后端实时调用。**请务必给部署加访问保护**，否则任何人都能用你的页面消耗别人/你的额度：
  - Vercel 项目 → Settings → **Deployment Protection**（Vercel Authentication / Password Protection）。
- 测试用过的 Key 建议在中转后台**轮换**。
- 切勿把含真实 Key 的 `providers.json` 提交进仓库（已在 `.gitignore` 忽略）。
