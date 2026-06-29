#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
E2B HTTP 补丁脚本 (e2b 2.20.0 + e2b_code_interpreter 2.4.1)
作用：把 SDK 所有对外连接从 https 降级为 http，适配自建/内网明文部署。
覆盖：
  1. e2b/connection_config.py                       (控制面 API，全局 https->http)
  2. e2b_code_interpreter/code_interpreter_sync.py  (run_code / jupyter)
  3. e2b_code_interpreter/code_interpreter_async.py (异步 run_code)
  4. e2b/volume/connection_config.py                (volume API，可选)
  5. e2b/sandbox/main.py                            (MCP URL，可选)
并清理 __pycache__，避免加载旧字节码。可重复执行。
"""
import sys
import shutil
from pathlib import Path


def get_site_packages():
    try:
        import e2b
        return Path(e2b.__file__).resolve().parent.parent
    except ImportError:
        print("错误: 未找到 e2b 包，请先安装: pip install e2b==2.20.0")
        sys.exit(1)


def backup_once(path: Path):
    bak = Path(str(path) + ".backup")
    if not bak.exists():
        shutil.copy2(path, bak)
        print(f"    已备份: {bak.name}")
    else:
        print(f"    备份已存在，跳过备份: {bak.name}")


def patch(path: Path, replacements, global_https=False, required=True):
    """replacements: list[(old, new)] 精确替换；global_https: 额外全局 https->http。
    返回 'patched' | 'skipped' | 'missing'"""
    label = path.name
    if not path.exists():
        if required:
            print(f"✗ [缺失] {path}")
        else:
            print(f"- [缺失] {path} (可选，忽略)")
        return "missing"

    content = original = path.read_text(encoding="utf-8")
    for old, new in replacements:
        if old in content:
            content = content.replace(old, new)
    if global_https:
        content = content.replace("https", "http")

    if content == original:
        print(f"○ [已是目标状态] {label}")
        return "skipped"

    print(f"→ 修改: {path}")
    backup_once(path)
    path.write_text(content, encoding="utf-8")
    print(f"✓ [完成] {label}")
    return "patched"


def clear_pycache(*roots: Path):
    print("\n清理 __pycache__ ...")
    for root in roots:
        if root.exists():
            for pc in root.rglob("__pycache__"):
                shutil.rmtree(pc, ignore_errors=True)
    print("✓ 已清理字节码缓存")


def main():
    print("=" * 60)
    print("E2B HTTP 补丁脚本")
    print("=" * 60)

    sp = get_site_packages()
    print(f"site-packages: {sp}")
    e2b_dir = sp / "e2b"
    ci_dir = sp / "e2b_code_interpreter"

    # 同步/异步两个文件里完全相同的协议三元表达式
    JUP = "'http' if self.connection_config.debug else 'https'"

    targets = [
        # (路径, 精确替换, 是否全局https替换, 是否必需)
        (e2b_dir / "connection_config.py", [], True, True),
        (ci_dir / "code_interpreter_sync.py",  [(JUP, "'http'")], False, True),
        (ci_dir / "code_interpreter_async.py", [(JUP, "'http'")], False, True),
        (e2b_dir / "volume" / "connection_config.py",
         [('f"https://api.{self.domain}"', 'f"http://api.{self.domain}"')], False, False),
        (e2b_dir / "sandbox" / "main.py",
         [('f"https://{self.get_host(self.mcp_port)}/mcp"',
           'f"http://{self.get_host(self.mcp_port)}/mcp"')], False, False),
    ]

    results = []
    missing_required = []
    for path, reps, glob, req in targets:
        print()
        status = patch(path, reps, global_https=glob, required=req)
        results.append((path.name, status))
        if status == "missing" and req:
            missing_required.append(path.name)

    clear_pycache(e2b_dir, ci_dir)

    print("\n" + "=" * 60)
    patched = sum(1 for _, s in results if s == "patched")
    skipped = sum(1 for _, s in results if s == "skipped")
    print(f"汇总: 本次修改 {patched} 个, 已是目标状态 {skipped} 个")
    if missing_required:
        print(f"⚠ 必需文件缺失 -> {missing_required}")
        print("✗ 补丁未完整应用")
    else:
        print("✓ 补丁应用完成，现在所有连接走 http")
    print("=" * 60)


if __name__ == "__main__":
    main()
