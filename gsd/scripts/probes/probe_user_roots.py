#!/usr/bin/env python3
"""看各种常见用法下 guest 里 uid 1000 进程树的形状：一种用法会产生几棵用户进程树、根是谁。

最初用来核对"多个根就拒绝"会不会误伤常见用法；gsd 现在对每棵树各 dump / restore 一次，不再拒绝，它留作观察工具。

在宿主机上跑（gsd 栈在运行；解释器要能 import 到带 checkpoint 接口的 SDK：PYTHONPATH=<代码仓>/py-sdk；凭据由本目录或上一级目录的 e2b_env.py 读取）：
    python3 probe_user_roots.py [模板名，默认 base-gsd]

脚本里"根"的判定是 gsd 规则的第一步（候选根）：uid 1000、父进程不是 uid 1000，且不是 PID 1/envd/gsd。
gsd 还会去掉祖先链上已有候选根的那些（用户进程 → sudo → 用户进程 的嵌套，见手册 08 章），这里没有做这一步。
只读探测，不调 checkpoint 接口；结束时关掉沙箱。
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from e2b_env import load_e2b_env
os.environ.update(load_e2b_env())
from e2b import Sandbox

TPL = sys.argv[1] if len(sys.argv) > 1 else "base-gsd"

# 在 guest 里算根：只用 /proc，不依赖 ps 的输出格式
ROOTS_SH = r'''
envd=$(systemctl show -p MainPID --value envd 2>/dev/null); gsd=$(systemctl show -p MainPID --value gsd 2>/dev/null)
for d in /proc/[0-9]*; do
  p=${d#/proc/}; u=$(awk '/^Uid:/{print $2}' $d/status 2>/dev/null) || continue
  [ "$u" = 1000 ] || continue
  [ "$p" = 1 ] || [ "$p" = "$envd" ] || [ "$p" = "$gsd" ] && continue
  s=$(cat $d/stat 2>/dev/null) || continue; s=${s##*) }; pp=$(echo $s | cut -d' ' -f2)
  pu=$(awk '/^Uid:/{print $2}' /proc/$pp/status 2>/dev/null)
  [ "$pu" = 1000 ] && [ "$pp" != 1 ] && [ "$pp" != "$envd" ] && [ "$pp" != "$gsd" ] && continue
  echo "ROOT pid=$p ppid=$pp comm=$(cat $d/comm)"
done
echo "--- uid 1000 进程（pid ppid stat comm args）"
ps -u 1000 -o pid=,ppid=,stat=,comm=,args= 2>/dev/null
echo "--- envd=$envd gsd=$gsd"
'''

def show(sb, title):
    out = sb.commands.run(ROOTS_SH, user="root", timeout=30).stdout
    roots = [l for l in out.splitlines() if l.startswith("ROOT ")]
    print(f"\n=== {title}：{len(roots)} 个根")
    print(out.rstrip())

sb = Sandbox.create(template=TPL, timeout=300)
try:
    print(f"[{TPL}] sandbox {sb.sandbox_id}")
    time.sleep(2)
    show(sb, "1. 空闲")
    sb.commands.run("sleep 600", background=True)
    time.sleep(1)
    show(sb, "2. commands.run 起一个后台命令（默认用户）")
    sb.commands.run("python3 -c 'import time; time.sleep(600)'", background=True)
    time.sleep(1)
    show(sb, "3. 再起第二个后台命令")
    sb.commands.run("nohup sleep 600 >/dev/null 2>&1 &", timeout=10)
    time.sleep(1)
    show(sb, "4. 前台命令里 nohup ... & 后退出（进程被收养）")
finally:
    sb.kill()
    print("\nsandbox killed")
