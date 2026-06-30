"""命令行入口：解析参数、构造渠道、运行评估、输出报告。"""

import argparse
import json
import sys

from .client import RelayClient
from .evaluator import evaluate
from .report import render_markdown, print_summary


def load_providers(args) -> list:
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


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="relay-purity-test",
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
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    providers = load_providers(args)
    if not providers:
        print("错误：请用 --config 提供配置，或用 --url/--key 指定单个渠道。",
              file=sys.stderr)
        print("示例：relay-purity-test --url https://your-relay.com/v1 "
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
