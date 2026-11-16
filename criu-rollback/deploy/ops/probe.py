"""在指定 conda 环境里查询模板或试建一个沙箱，输出一行 JSON 供 flow.py 判读。不输出凭据。

    python ops/probe.py templates        列出模板（别名、build ID、状态），同时确认 API 和 key 可用
    python ops/probe.py create <模板>    建一个沙箱后立即删除，确认服务就绪且模板可用
    python ops/probe.py cleanup <模板>   删除由这个模板起的全部沙箱（只用于本包的测试模板）
"""
import json
import os
from pathlib import Path
import sys
import time
import urllib.request

import paths
sys.path.insert(0, str(paths.SCRIPTS))
from e2b_env import load_e2b_env


def emit(**data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


def cleanup(alias, templates):
    from e2b import Sandbox
    ids = {t.get("templateID") for t in templates if alias in (t.get("aliases") or [])}
    killed = []
    paginator = Sandbox.list()
    while paginator.has_next:
        for info in paginator.next_items():
            if info.template_id in ids or info.name == alias:
                Sandbox.kill(info.sandbox_id)
                killed.append(info.sandbox_id)
    emit(ok=True, killed=killed)
    return 0


def main():
    try:
        os.environ.update(load_e2b_env())
    except SystemExit as exc:
        emit(ok=False, error="客户端凭据: %s" % exc)
        return 1
    if sys.argv[1] in ("templates", "cleanup"):
        url = os.environ["E2B_API_URL"].rstrip("/") + "/templates"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(urllib.request.Request(url, headers={"X-API-Key": os.environ["E2B_API_KEY"]}), timeout=10) as r:
                data = json.load(r)
        except Exception as exc:
            emit(ok=False, error="%s: %s" % (type(exc).__name__, exc))
            return 1
        if sys.argv[1] == "cleanup":
            return cleanup(sys.argv[2], data)
        emit(ok=True, templates=[{"aliases": t.get("aliases") or [], "templateID": t.get("templateID"),
                                  "buildID": t.get("buildID"), "buildStatus": t.get("buildStatus")} for t in data])
        return 0
    from e2b import Sandbox
    started = time.time()
    try:
        sandbox = Sandbox.create(template=sys.argv[2], timeout=60)
    except Exception as exc:
        message = "%s: %s" % (type(exc).__name__, exc)
        emit(ok=False, error=message, not_found="NotFound" in type(exc).__name__ or " 404" in message)
        return 0
    elapsed = round((time.time() - started) * 1000)
    try:
        sandbox.kill()
    except Exception:
        pass
    emit(ok=True, ms=elapsed, sandbox=sandbox.sandbox_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
