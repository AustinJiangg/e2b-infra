"""
E2B 快照（snapshot）功能验证脚本。

覆盖四件事：
  1. sync / async 两种 SDK 用法下的「打快照 -> 关沙箱 -> 从快照 fork 新沙箱」；
  2. pause / resume（一对一暂停恢复，沙箱 ID 不变），和快照的一对多 fork 是两回事；
  3. diff 增量快照探针：同一沙箱连打三次快照，用产物大小判断增量是否生效；
  4. cleanup：清掉账号下残留的快照和 paused 沙箱。

前置条件（与 benchmark/README.md 第 2 节一致）：
    pip install e2b==2.20.0 python-dotenv
    python /opt/e2b-infra/patch_e2b.py          # https -> http 补丁
    cp .env.example .env && bash sync-env.sh    # 或手工填 E2B_API_KEY / E2B_API_URL 等

运行示例：
    # 全跑一遍（sync + async + pause + diff，不含 cleanup），跑完自动删快照
    python snapshot.py

    # 只跑同步版快照 demo
    python snapshot.py --mode sync

    # 只跑异步版（两个 fork 并发拉起）
    python snapshot.py --mode async

    # 只跑 pause/resume
    python snapshot.py --mode pause

    # 增量快照探针：写 200MB 随机数据，跑完保留快照好去存储里量大小
    python snapshot.py --mode diff --keep-snapshot --blob-mb 200

    # 增量探针的轻量版（写 20MB，快一些）
    python snapshot.py --mode diff --keep-snapshot --blob-mb 20

    # 换模板
    python snapshot.py --mode sync --template-id my-template

    # 查看快照（--keep-snapshot 跑完之后先看有哪些）
    python snapshot.py --mode cleanup --dry-run

    # 删除快照（全量清理：所有快照 + 所有 paused 沙箱，注意别误删要留的）
    python snapshot.py --mode cleanup

diff 模式怎么看结果：
    脚本本身只打印三个快照 ID 和耗时，耗时只是弱信号；真正的判据是去节点上比这三个
    build 的产物字节数，所以要带 --keep-snapshot 跑。量哪两处，别混：

        # 本地缓存（任何 STORAGE_PROVIDER 下都有，orchestrator 重启会清空）
        ll -th /orchestrator/build | head       # <buildID>-memfile-xxx / <buildID>-rootfs.ext4-xxx
        ll -th /orchestrator/template | head    # <buildID>/cache/<uuid>/ 下是 snapfile + metadata.json

        # 持久化存储（由 STORAGE_PROVIDER 决定；本部署是 Local ⇒ 默认 /tmp/templates）
        du -sh /tmp/templates/*/ | tail

    先确认实际生效的 provider（以进程环境为准，不要靠猜）：
        tr '\0' '\n' < /proc/$(pgrep -x orchestrator | head -1)/environ \
          | grep -E 'STORAGE_PROVIDER|LOCAL_.*_BASE_PATH|MINIO_'

    模板/快照到底存在哪、为什么 .env 里的 MinIO 没生效、想换成 MinIO 怎么改，
    见 deploy-docs/10-模板与快照存储位置梳理.md。
    增量快照的实现原理、x86 与 ARM 版的差异、本脚本在 ARM 上的实测分析，
    见 deploy-docs/09-增量快照实现与ARM实测分析.md。
"""

from dotenv import load_dotenv

load_dotenv()

import asyncio
import argparse
import time

from e2b import Sandbox, AsyncSandbox, SandboxQuery, CommandExitException
from e2b.api.client.models.sandbox_state import SandboxState


DEFAULT_TEMPLATE_ID = "base"

STATE_FILE = "/home/user/state.txt"
MARKER_FILE = "/home/user/work/marker.txt"
BLOB_FILE = "/home/user/blob.bin"
PAYLOAD = "hello-from-origin"
# 快照大沙箱可能超过 SDK 默认的 60s HTTP 超时，这里放宽
SNAPSHOT_REQUEST_TIMEOUT = 300


def print_result(tag: str, cmd: str, output: str) -> None:
    output = output.strip()
    if "\n" in output:
        print(f"[{tag}] $ {cmd}:\n{output}")
    else:
        print(f"[{tag}] $ {cmd} -> {output}")


def run_cmd(sbx, tag: str, cmd: str) -> str:
    """
    commands.run 在退出码非 0 时会直接抛 CommandExitException，
    这里统一兜住，返回输出而不是让 demo 中断
    """
    try:
        result = sbx.commands.run(cmd, timeout=120)
        output = result.stdout
    except CommandExitException as e:
        output = f"(exit {e.exit_code}) {e.stderr}"
    print_result(tag, cmd, output)
    return output


async def run_cmd_async(sbx, tag: str, cmd: str) -> str:
    try:
        result = await sbx.commands.run(cmd, timeout=120)
        output = result.stdout
    except CommandExitException as e:
        output = f"(exit {e.exit_code}) {e.stderr}"
    print_result(tag, cmd, output)
    return output


def run_sync_snapshot(
    template_id: str = DEFAULT_TEMPLATE_ID,
    keep_snapshot: bool = False,
) -> None:
    """
    同步版：创建沙箱 -> 改状态 -> 打快照 -> 关掉沙箱 -> 从快照 fork 出新沙箱
    """
    snapshot_id = None
    origin = None
    forks = []

    try:
        # 1. 创建沙箱，制造一点只有改动过才有的状态：写文件 + 新建目录
        origin = Sandbox.create(template=template_id)
        print(f"[Sync] Sandbox created with ID: {origin.sandbox_id}")

        origin.files.write(STATE_FILE, PAYLOAD)
        run_cmd(origin, "Sync", f"mkdir -p $(dirname {MARKER_FILE}) && echo {PAYLOAD} > {MARKER_FILE} && cat {MARKER_FILE}")

        # 2. 打快照。快照期间沙箱会被短暂 pause，完成后自动回到 running，ID 不变
        snap = origin.create_snapshot(request_timeout=SNAPSHOT_REQUEST_TIMEOUT)
        snapshot_id = snap.snapshot_id
        print(f"[Sync] Snapshot created: {snapshot_id}")
        print(f"[Sync] 快照后原沙箱状态: {Sandbox.get_info(origin.sandbox_id).state}")
        run_cmd(origin, "Sync", "echo 快照后原沙箱仍然可用")

        # 3. 列出这个沙箱名下的快照
        paginator = Sandbox.list_snapshots(sandbox_id=origin.sandbox_id, limit=10)
        while paginator.has_next:
            for info in paginator.next_items():
                print(f"[Sync] list_snapshots -> {info.snapshot_id}")

        # 4. 关掉原沙箱。快照是持久对象，不会跟着一起消失
        origin.kill()
        print(f"[Sync] 原沙箱已关闭: {origin.sandbox_id}")
        origin = None

        # 5. 从快照 fork 两个新沙箱。snapshot_id 就是带 tag 的 template id，
        #    直接放在 template 参数位，Sandbox.create 没有 snapshot_id 这个参数
        for i in range(2):
            fork = Sandbox.create(template=snapshot_id)
            forks.append(fork)
            print(f"[Sync] Fork-{i} created from snapshot: {fork.sandbox_id}")
            print_result(f"Sync fork-{i}", "cat state.txt", fork.files.read(STATE_FILE))
            run_cmd(fork, f"Sync fork-{i}", f"cat {MARKER_FILE}")

    finally:
        # 6. 收尾：关沙箱 + 删快照
        for fork in forks:
            fork.kill()
            print(f"[Sync] Fork 已关闭: {fork.sandbox_id}")
        if origin is not None:
            origin.kill()
            print(f"[Sync] 原沙箱已关闭: {origin.sandbox_id}")
        if snapshot_id and not keep_snapshot:
            deleted = Sandbox.delete_snapshot(snapshot_id)
            print(f"[Sync] delete_snapshot({snapshot_id}) -> {deleted}")
        elif snapshot_id:
            print(f"[Sync] 保留快照: {snapshot_id}")


def run_pause_resume(template_id: str = DEFAULT_TEMPLATE_ID) -> None:
    """
    pause / resume：一对一的暂停恢复，沙箱 ID 不变，和 snapshot 的一对多 fork 是两回事
    """
    sbx = None

    try:
        sbx = Sandbox.create(template=template_id)
        print(f"[Pause] Sandbox created with ID: {sbx.sandbox_id}")
        sbx.files.write(STATE_FILE, PAYLOAD)

        # 1. 暂停。文件系统 + 内存状态都会保留，沙箱停止占用计算资源
        sbx.pause()
        print(f"[Pause] 暂停后状态: {Sandbox.get_info(sbx.sandbox_id).state}")

        # 2. 列出所有 paused 沙箱
        paginator = Sandbox.list(query=SandboxQuery(state=[SandboxState.PAUSED]), limit=10)
        for info in paginator.next_items():
            print(f"[Pause] paused sandbox -> {info.sandbox_id}")

        # 3. 恢复。没有独立的 resume()，connect() 碰到 paused 沙箱会自动把它拉起来
        sbx = Sandbox.connect(sbx.sandbox_id)
        print(f"[Pause] 恢复后状态: {Sandbox.get_info(sbx.sandbox_id).state}，ID 不变")
        print_result("Pause", "cat state.txt", sbx.files.read(STATE_FILE))

    finally:
        if sbx is not None:
            sbx.kill()
            print(f"[Pause] 沙箱已关闭: {sbx.sandbox_id}")


def run_incremental_probe(
    template_id: str = DEFAULT_TEMPLATE_ID,
    keep_snapshot: bool = False,
    blob_mb: int = 200,
) -> None:
    """
    增量快照探针：对同一个沙箱连打三次快照
      #1 基线
      #2 中间什么都不做
      #3 写入 blob_mb 大小的随机数据之后
    如果是增量，#2 的产物应该极小，#3 比 #2 大约多出 blob_mb。
    耗时只是弱信号，真正的判据是去存储里比这三个 build 目录的字节数，
    所以想量存储的话要带 --keep-snapshot 跑。
    """
    sbx = None
    snapshot_ids = []

    def take(label: str) -> str:
        t0 = time.perf_counter()
        snap = sbx.create_snapshot(request_timeout=SNAPSHOT_REQUEST_TIMEOUT)
        print(f"[Diff] {label}: {snap.snapshot_id}  耗时 {time.perf_counter() - t0:.2f}s")
        return snap.snapshot_id

    try:
        sbx = Sandbox.create(template=template_id)
        print(f"[Diff] Sandbox created with ID: {sbx.sandbox_id}")

        snapshot_ids.append(take("#1 基线"))
        snapshot_ids.append(take("#2 无任何改动"))

        # 用 /dev/urandom 而不是 /dev/zero，否则全零页会被去重掉，测不出增量
        run_cmd(sbx, "Diff", f"dd if=/dev/urandom of={BLOB_FILE} bs=1M count={blob_mb} 2>&1 | tail -1 && sync")
        run_cmd(sbx, "Diff", f"du -h {BLOB_FILE}")
        snapshot_ids.append(take(f"#3 写入 {blob_mb}MB 之后"))

        print("[Diff] 去存储里比这三个 build 目录的总字节数：")
        for i, sid in enumerate(snapshot_ids, 1):
            print(f"[Diff]   #{i} {sid}")
        print(f"[Diff] 增量的话 #2 应该极小，#3 比 #2 多出约 {blob_mb}MB")

    finally:
        if sbx is not None:
            sbx.kill()
            print(f"[Diff] 沙箱已关闭: {sbx.sandbox_id}")
        for sid in snapshot_ids:
            if keep_snapshot:
                print(f"[Diff] 保留快照: {sid}")
            else:
                print(f"[Diff] delete_snapshot({sid}) -> {Sandbox.delete_snapshot(sid)}")


def run_cleanup(dry_run: bool = False) -> None:
    """
    清理：删掉账号下所有快照 + 干掉所有 paused 沙箱
    --keep-snapshot 跑完之后用这个收尾。注意是全量清理，别误删你要留的快照
    """
    snapshot_ids = []
    paginator = Sandbox.list_snapshots(limit=100)
    while paginator.has_next:
        snapshot_ids.extend(info.snapshot_id for info in paginator.next_items())

    paused_ids = []
    sbx_paginator = Sandbox.list(query=SandboxQuery(state=[SandboxState.PAUSED]), limit=100)
    while sbx_paginator.has_next:
        paused_ids.extend(info.sandbox_id for info in sbx_paginator.next_items())

    print(f"[Cleanup] 找到 {len(snapshot_ids)} 个快照，{len(paused_ids)} 个 paused 沙箱")

    if dry_run:
        for sid in snapshot_ids:
            print(f"[Cleanup] (dry-run) snapshot {sid}")
        for sid in paused_ids:
            print(f"[Cleanup] (dry-run) paused sandbox {sid}")
        return

    for sid in snapshot_ids:
        print(f"[Cleanup] delete_snapshot({sid}) -> {Sandbox.delete_snapshot(sid)}")
    for sid in paused_ids:
        print(f"[Cleanup] kill({sid}) -> {Sandbox.kill(sid)}")


async def run_async_snapshot(
    template_id: str = DEFAULT_TEMPLATE_ID,
    keep_snapshot: bool = False,
) -> None:
    """
    异步版：同样的流程，全部换成 await，两个 fork 并发拉起
    """
    snapshot_id = None
    origin = None
    forks = []

    try:
        origin = await AsyncSandbox.create(template=template_id)
        print(f"[Async] Sandbox created with ID: {origin.sandbox_id}")

        await origin.files.write(STATE_FILE, PAYLOAD)
        await run_cmd_async(origin, "Async", f"mkdir -p $(dirname {MARKER_FILE}) && echo {PAYLOAD} > {MARKER_FILE} && cat {MARKER_FILE}")

        snap = await origin.create_snapshot(request_timeout=SNAPSHOT_REQUEST_TIMEOUT)
        snapshot_id = snap.snapshot_id
        print(f"[Async] Snapshot created: {snapshot_id}")
        info = await AsyncSandbox.get_info(origin.sandbox_id)
        print(f"[Async] 快照后原沙箱状态: {info.state}")

        await origin.kill()
        print(f"[Async] 原沙箱已关闭: {origin.sandbox_id}")
        origin = None

        async def fork_from_snapshot(index: int):
            fork = await AsyncSandbox.create(template=snapshot_id)
            print(f"[Async] Fork-{index} created from snapshot: {fork.sandbox_id}")
            content = await fork.files.read(STATE_FILE)
            print_result(f"Async fork-{index}", "cat state.txt", content)
            await run_cmd_async(fork, f"Async fork-{index}", f"cat {MARKER_FILE}")
            return fork

        forks = await asyncio.gather(*(fork_from_snapshot(i) for i in range(2)))

    finally:
        for fork in forks:
            await fork.kill()
            print(f"[Async] Fork 已关闭: {fork.sandbox_id}")
        if origin is not None:
            await origin.kill()
            print(f"[Async] 原沙箱已关闭: {origin.sandbox_id}")
        if snapshot_id and not keep_snapshot:
            deleted = await AsyncSandbox.delete_snapshot(snapshot_id)
            print(f"[Async] delete_snapshot({snapshot_id}) -> {deleted}")
        elif snapshot_id:
            print(f"[Async] 保留快照: {snapshot_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="E2B Sandbox snapshot demo script")
    parser.add_argument(
        "--template-id",
        default=DEFAULT_TEMPLATE_ID,
        help="Sandbox template ID",
    )
    parser.add_argument(
        "--mode",
        choices=["sync", "async", "pause", "diff", "cleanup", "all"],
        default="all",
        help="sync/async = 快照 demo，pause = 暂停恢复，diff = 增量快照探针，cleanup = 清理残留，all = 全跑（不含 cleanup）",
    )
    parser.add_argument(
        "--keep-snapshot",
        action="store_true",
        help="跑完保留快照，默认跑完删除",
    )
    parser.add_argument(
        "--blob-mb",
        type=int,
        default=200,
        help="diff 模式下写入的随机数据大小（MB）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="cleanup 模式下只列出不删除",
    )
    args = parser.parse_args()

    if args.mode in ("sync", "all"):
        run_sync_snapshot(args.template_id, args.keep_snapshot)
    if args.mode in ("async", "all"):
        asyncio.run(run_async_snapshot(args.template_id, args.keep_snapshot))
    if args.mode in ("pause", "all"):
        run_pause_resume(args.template_id)
    if args.mode in ("diff", "all"):
        run_incremental_probe(args.template_id, args.keep_snapshot, args.blob_mb)
    if args.mode == "cleanup":
        run_cleanup(args.dry_run)
