"""公共模块：给本目录及各子目录（acceptance/、stress/、probes/）的脚本读取 e2b 客户端连接参数与凭据。

各脚本在自身所在目录和上一级目录里找本文件，所以它放在 scripts/ 下即可被三个子目录共用。

每个值的取值顺序：
  1. 已经导出的环境变量（E2B_API_URL、E2B_DOMAIN、E2B_HTTP_SSL、E2B_ACCESS_TOKEN、E2B_API_KEY）
  2. 与本文件同目录的 `.env`（KEY=VALUE 每行一个；含凭据，不入库）
  3. `e2b auth login` 写出的 ~/.e2b/config.json（accessToken / teamApiKey；路径可用环境变量 E2B_CONFIG 换）
  4. 内置默认值：API 在本机 3000 端口、明文 HTTP、域名 e2b.app（脚本都在宿主机上跑；
     API 不在本机时导出 E2B_API_URL 或写进 .env）
"""
import json
import os
from pathlib import Path

DEFAULTS = {
    "E2B_API_URL": "http://localhost:3000",
    "E2B_DOMAIN": "e2b.app",
    "E2B_HTTP_SSL": "false",
}


def load_e2b_env(api_url: str | None = None, config_path: str | None = None) -> dict[str, str]:
    env_file = Path(__file__).resolve().parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

    if api_url:
        os.environ["E2B_API_URL"] = api_url
    for key, value in DEFAULTS.items():
        os.environ.setdefault(key, value)

    if not (os.environ.get("E2B_ACCESS_TOKEN") and os.environ.get("E2B_API_KEY")):
        config = Path(config_path or os.environ.get("E2B_CONFIG", "~/.e2b/config.json")).expanduser()
        if config.exists():
            data = json.loads(config.read_text(encoding="utf-8"))
            if data.get("accessToken"):
                os.environ.setdefault("E2B_ACCESS_TOKEN", data["accessToken"])
            if data.get("teamApiKey"):
                os.environ.setdefault("E2B_API_KEY", data["teamApiKey"])

    missing = [k for k in ("E2B_ACCESS_TOKEN", "E2B_API_KEY") if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"missing {', '.join(missing)}: export them, put them in {env_file}, "
            "or run `e2b auth login` so ~/.e2b/config.json exists"
        )
    return {k: os.environ[k] for k in ("E2B_API_URL", "E2B_DOMAIN", "E2B_HTTP_SSL")}
