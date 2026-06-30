"""purity —— 聚合平台 / API 中转资源纯度测试核心库。

模块划分：
  transport  : 基于 curl 的零依赖 HTTP 传输（Resp / curl_request）
  client     : 中转客户端（RelayClient，OpenAI + Anthropic 双协议）
  util       : 文本工具（norm / contains_any / clip）
  probes     : 6 个纯度探针（probe_* / DimResult / PROBES）
  evaluator  : 渠道评估汇总（ProviderResult / evaluate）
  report     : Markdown 报告与控制台总览（render_markdown / print_summary）
  cli        : 命令行入口（main / parse_args / load_providers）
"""

from .transport import Resp, curl_request
from .client import RelayClient, extract_openai_text, extract_anthropic_text
from .util import norm, contains_any, clip
from .probes import (
    DimResult,
    VERDICT_ICON,
    PROBES,
    probe_protocol,
    probe_freshness,
    probe_reasoning,
    probe_instruction,
    probe_stream,
    probe_leakage,
)
from .evaluator import ProviderResult, evaluate
from .report import render_markdown, print_summary
from .cli import main, parse_args, load_providers

__version__ = "1.0.0"

__all__ = [
    "Resp", "curl_request",
    "RelayClient", "extract_openai_text", "extract_anthropic_text",
    "norm", "contains_any", "clip",
    "DimResult", "VERDICT_ICON", "PROBES",
    "probe_protocol", "probe_freshness", "probe_reasoning",
    "probe_instruction", "probe_stream", "probe_leakage",
    "ProviderResult", "evaluate",
    "render_markdown", "print_summary",
    "main", "parse_args", "load_providers",
    "__version__",
]
