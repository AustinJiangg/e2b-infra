"""handbook_extract 的本地单测：空目录与缺文件时不抛异常、标明“缺”；分档表按三轮取中位数。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import handbook_extract


def run_dir(tmp):
    acc = Path(tmp) / "acceptance"
    acc.mkdir()
    for i, base in enumerate((10.0, 30.0, 20.0), 1):
        tier = {"mem_mb": 12, "file_kb": 4096, "sdk": {"total_ms": base}, "e2e_ms": base + 1, "rss_bytes": 1048576 * 25}
        data = {"schema_version": 2, "measurement_mode": "per-tier-size", "restore_path": "x",
                "baseline": [tier], "create": [tier], "restore": [dict(tier, ok=True)], "max_sdk_journal_diff_ms": 0.0}
        (acc / ("perf-run%d.json" % i)).write_text(json.dumps(data))
    return Path(tmp)


class ExtractTests(unittest.TestCase):
    def capture(self, run):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            handbook_extract.main(run)
        return out.getvalue()

    def test_empty_run_reports_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = self.capture(tmp)
        self.assertIn("==== 结束 ====", text)
        self.assertIn("缺", text)
        self.assertNotIn("出错", text)

    def test_perf_median_of_three(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = self.capture(run_dir(tmp))
        self.assertIn("  16 | 12/4 | 20.0 20.0 20.0 | 21.0 21.0 21.0", text)
        self.assertIn("  16 | 10.0 30.0 20.0 |", text)
        self.assertIn("恢复校验 1/1", text)


if __name__ == "__main__":
    unittest.main()
