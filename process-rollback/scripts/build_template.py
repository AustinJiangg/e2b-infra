#!/usr/bin/env python3
"""从镜像仓库里的一个镜像构建 e2b 模板（对 SDK 的 Template.build 的一层封装）。

gsd 栈下建模板不需要任何 gsd 专有参数——往 guest 里注入 gsd 与 overlay-init 是 orchestrator 自动做的。
镜像里要带 CRIU 4.2.1。

用法（在 scripts/ 目录下；解释器要能 import 到带 checkpoint 接口的 SDK，即 PYTHONPATH=<代码仓>/py-sdk）：
    python build_template.py --image <镜像仓库>/e2b-orchestration/ubuntu:22.04-custom-criu421
    python build_template.py --alias base-gsd --memory 2048 --image <镜像>      # 等价的完整写法
    python build_template.py --alias <调试模板名> --memory 4096 --image <镜像>   # 调试内核用的模板

默认值：
    --alias   base-gsd（手册统一使用的模板名）
    --image   环境变量 GSD_TEMPLATE_IMAGE；没设时是 harbor:443/e2b-orchestration/ubuntu:22.04-custom-criu421
    --cpu 2、--memory 2048（手册全部实测所用的规格）
    不带 --use-cache 时不使用构建缓存（skip_cache=True）

凭据来自 E2B_* 环境变量（或本目录的 .env），都没有时退到 `e2b auth login` 写出的 ~/.e2b/config.json；
规则见同目录的 e2b_env.py。
"""
import argparse
import os
import sys
import time

from e2b_env import load_e2b_env


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an e2b template from a registry image")
    parser.add_argument(
        "--image",
        default=os.environ.get("GSD_TEMPLATE_IMAGE", "harbor:443/e2b-orchestration/ubuntu:22.04-custom-criu421"),
        help="OCI image reference the template is built from",
    )
    parser.add_argument("--alias", default="base-gsd", help="template alias (default: base-gsd)")
    parser.add_argument("--api-url", default=None, help="E2B API URL (default: $E2B_API_URL or http://localhost:3000)")
    parser.add_argument("--cpu", type=int, default=2, help="vCPU count (default: 2)")
    parser.add_argument("--memory", type=int, default=2048, help="memory in MB (default: 2048)")
    parser.add_argument("--config", default=None, help="path to e2b config.json (default: ~/.e2b/config.json)")
    parser.add_argument("--use-cache", action="store_true", help="allow the template build cache")
    args = parser.parse_args()

    try:
        settings = load_e2b_env(api_url=args.api_url, config_path=args.config)
    except SystemExit as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    from e2b import Template, default_build_logger

    print(f"building template alias={args.alias} image={args.image} cpu={args.cpu} memory={args.memory}MB "
          f"api={settings['E2B_API_URL']}", flush=True)
    started = time.time()
    Template.build(
        Template().from_dockerfile(f"FROM {args.image}"),
        alias=args.alias,
        cpu_count=args.cpu,
        memory_mb=args.memory,
        on_build_logs=default_build_logger(),
        skip_cache=not args.use_cache,
    )
    print(f"template '{args.alias}' built in {time.time() - started:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
