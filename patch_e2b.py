#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
E2B 2.20.0 Patch Script
1. 修改 connection_config.py: https -> http
"""

import os
import sys
import shutil
from pathlib import Path

def find_e2b_path():
    """查找 e2b 2.20.0 的安装路径"""
    try:
        # 方法1: 使用 importlib.metadata (Python 3.8+)
        try:
            from importlib.metadata import distribution
            dist = distribution("e2b")
            e2b_path = dist.locate_file("")
            if e2b_path and "e2b" in str(e2b_path):
                return Path(e2b_path)
        except Exception:
            pass

        # 方法2: 使用 pkg_resources
        try:
            import pkg_resources
            dist = pkg_resources.get_distribution("e2b")
            if dist.version == "2.20.0":
                return Path(dist.location) / "e2b"
            else:
                print(f"警告: 找到 e2b {dist.version}，但需要的是 2.20.0")
                return Path(dist.location) / "e2b"
        except Exception:
            pass

        # 方法3: 直接 import 查找
        import e2b
        return Path(e2b.__file__).parent

    except ImportError:
        print("错误: 未找到 e2b 包，请先安装: pip install e2b==2.20.0")
        sys.exit(1)

def backup_file(file_path):
    """创建备份文件"""
    backup_path = str(file_path) + ".backup"
    shutil.copy2(file_path, backup_path)
    print(f"已创建备份: {backup_path}")
    return backup_path

def modify_connection_config(file_path):
    """修改 connection_config.py: https -> http"""
    print(f"\n处理文件: {file_path}")

    if not file_path.exists():
        print(f"错误: 文件不存在 {file_path}")
        return False

    content = file_path.read_text(encoding='utf-8')
    original_content = content

    https_count = content.count('https')

    content = content.replace("https", "http")

    if content == original_content:
        return False

    # 备份并写入
    backup_file(file_path)
    file_path.write_text(content, encoding='utf-8')
    return True

def main():
    print("=" * 60)
    print("E2B 2.20.0 补丁脚本")
    print("=" * 60)

    # 1. 查找 e2b 路径
    print("\n[1/3] 查找 e2b 安装路径...")
    e2b_path = find_e2b_path()
    print(f"找到路径: {e2b_path}")

    # 2. 查找文件
    print("\n[2/3] 查找目标文件...")

    # 查找 connection_config.py
    conn_config = None
    for root, dirs, files in os.walk(e2b_path):
        if "connection_config.py" in files:
            conn_config = Path(root) / "connection_config.py"
            break

    if conn_config:
        print(f"✓ 找到 connection_config.py: {conn_config}")
    else:
        print("✗ 未找到 connection_config.py，尝试直接路径")
        conn_config = e2b_path / "connection_config.py"

    # 3. 执行修改
    print("\n[3/3] 执行修改...")

    success_count = 0
    if conn_config and conn_config.exists():
        if modify_connection_config(conn_config):
            success_count += 1
    else:
        print(f"错误: connection_config.py 不存在: {conn_config}")

    print("\n" + "=" * 60)
    if success_count == 2:
        print("✓ 所有补丁应用成功！")
    elif success_count == 1:
        print("△ 部分补丁应用成功")
    else:
        print("✗ 补丁应用失败")
    print("=" * 60)


if __name__ == "__main__":
    main()
