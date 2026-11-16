"""离线执行 SDK 自带的七个鉴权头测试，不发 HTTP 请求、不读取现场凭据。"""
from pathlib import Path
import runpy

root = Path(__file__).resolve().parents[1]
tests = runpy.run_path(str(root / "sdk/test_gsd_headers.py"))
count = 0
for name, fn in sorted(tests.items()):
    if name.startswith("test_") and callable(fn):
        fn()
        count += 1
assert count == 7, count
print("SDK header tests: PASS 7 / FAIL 0")
