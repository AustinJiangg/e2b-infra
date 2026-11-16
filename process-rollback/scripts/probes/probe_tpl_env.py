#!/usr/bin/env python3
"""对比两个模板的 guest 环境（vCPU、内存、THP、内核、挂载、服务），定位模板级性能差异。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from e2b_env import load_e2b_env
os.environ.update(load_e2b_env())
from e2b import Sandbox

CMD = r'''
echo "nproc: $(nproc)"; grep -E 'MemTotal|MemAvailable|AnonHugePages|ShmemHugePages' /proc/meminfo
echo "thp: $(cat /sys/kernel/mm/transparent_hugepage/enabled) defrag: $(cat /sys/kernel/mm/transparent_hugepage/defrag) shmem: $(cat /sys/kernel/mm/transparent_hugepage/shmem_enabled)"
echo "kernel: $(uname -r) $(cat /proc/version | md5sum | cut -c1-8)"; cat /proc/cmdline
echo "rcu_expedited: $(cat /sys/kernel/rcu_expedited)"
sha256sum /usr/sbin/criu /usr/bin/gsd /usr/bin/envd 2>/dev/null | cut -c1-16
findmnt -rn -o TARGET,FSTYPE,OPTIONS | grep -vE '^/(sys|proc|dev)' | head -30
systemctl list-units --type=service --state=running --no-legend | awk '{print $1}' | tr '\n' ' '; echo
cat /sys/fs/cgroup/cgroup.subtree_control; sysctl kernel.pid_max vm.overcommit_memory kernel.sched_autogroup_enabled 2>/dev/null
'''
for tpl in sys.argv[1:]:
    sb = Sandbox.create(template=tpl, timeout=300)
    try:
        print(f"===== {tpl} ({sb.sandbox_id})")
        print(sb.commands.run(CMD, user="root").stdout)
    finally:
        sb.kill()
