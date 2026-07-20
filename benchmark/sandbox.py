from dotenv import load_dotenv
load_dotenv()
import asyncio
import argparse
from e2b import Sandbox, AsyncSandbox

DEFAULT_TEMPLATE_ID = "base"


def run_sync_sandbox(template_id: str = DEFAULT_TEMPLATE_ID) -> None:
    """
    使用同步 Sandbox 的上下文管理器，自动创建与关闭沙箱
    """

    with Sandbox.create(template=template_id) as sbx:
        print(f"[Sync] Sandbox created with ID: {sbx.sandbox_id}")
        print(f"[Sync] Envd API URL: {sbx.envd_api_url}")

        for cmd in ["pwd", "whoami", "df -h", "python3 --version"]:
            result = sbx.commands.run(cmd)
            output = result.stdout.strip()
            if '\n' in output:
                print(f"$ {cmd}:\n{output}")
            else:
                print(f"$ {cmd} -> {output}")

    # 退出 with 块后沙箱自动关闭
    print(f"[Sync] 沙箱已自动关闭: {sbx.sandbox_id}")


async def run_async_sandbox(template_id: str = DEFAULT_TEMPLATE_ID) -> None:
    """
    使用异步 Sandbox 的上下文管理器，自动创建与关闭沙箱
    """
    sbx = await AsyncSandbox.create(template=template_id)

    async with sbx:
        print(f"[Async] Sandbox created with ID: {sbx.sandbox_id}")
        print(f"[Async] Envd API URL: {sbx.envd_api_url}")

        for cmd in ["pwd", "whoami", "df -h"]:
            result = await sbx.commands.run(cmd)
            output = result.stdout.strip()
            if '\n' in output:
                print(f"$ {cmd}:\n{output}")
            else:
                print(f"$ {cmd} -> {output}")

    # 退出 async with 块后沙箱自动关闭
    print(f"[Async] Sandbox 已自动关闭: {sbx.sandbox_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="E2B Sandbox demo script")
    parser.add_argument(
        "--template-id",
        default=DEFAULT_TEMPLATE_ID,
        help="Sandbox template ID",
    )
    args = parser.parse_args()

    run_sync_sandbox(args.template_id)
    asyncio.run(run_async_sandbox(args.template_id))
