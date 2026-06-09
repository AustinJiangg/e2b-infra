# 使用e2b-infra

## 使用说明

### 客户端环境配置

安装 Python 依赖：

```bash
pip install e2b==2.15.3
pip install e2b_code_interpreter==2.4.1
python3 /opt/e2b-infra/patch_e2b.py
```

创建 `.env` 文件：

```env
E2B_ACCESS_TOKEN="sk_e2b_xxx"
E2B_API_KEY="e2b_xxx"
E2B_DOMAIN="xxx"                                               # Client Proxy 域名（3002 端口）
E2B_API_URL="http://{server_ip}:3000"                          # API 地址（3000 端口）
E2B_HTTP_SSL="false"
```

### 与集群交互

#### 构建模板

示例代码

```python
# build_prod.py
from dotenv import load_dotenv
load_dotenv()
import os

from e2b import Template, default_build_logger

if __name__ == '__main__':
    Template.build(
        #基础沙箱模板示例代码
        Template().from_dockerfile('FROM harbor:443/e2b-orchestration/ubuntu:22.04-custom'),
        #代码沙箱模板示例代码
        #Template().from_dockerfile('FROM harbor:443/e2b-orchestration/code-interpreter:v1').set_start_cmd("sudo /root/.jupyter/start-up.sh", wait_for_url("http://localhost:49999/health")),
        alias="base",
        cpu_count=1,
        memory_mb=1024,
        on_build_logs=default_build_logger()
    )
```

#### 启动沙箱

使用沙箱执行命令（示例代码）

```python
from dotenv import load_dotenv
load_dotenv()
import os

from e2b import Sandbox

sbx = Sandbox.create("basev61")
print(sbx.commands.run("whoami"))  # guest
print(sbx.commands.run("pwd"))  # /home/guest

sbx.kill()
```

示例：

```bash
[root@hostname-api home]# python3 test.py 
CommandResult(stderr='', stdout='user\n', exit_code=0, error='')
CommandResult(stderr='', stdout='/home/user\n', exit_code=0, error='')
```

#### 代码沙箱

使用沙箱执行命令（示例代码）

```python
from dotenv import load_dotenv
load_dotenv()
import os
from e2b_code_interpreter import Sandbox

sbx = Sandbox.create("code-interpreter-v9")
execution = sbx.run_code('print("Hello, world!")')
print(execution)
print(sbx.kill())
```

示例：

```bash
[root@hostname-api home]# python3 run_code.py
Execution(Results: [], Logs: Logs(stdout: ['Hello, world!\n'], stderr: []), Error: None)
```

#### 构建模板用的镜像

拉取官方镜像模板：

```bash
git clone https://github.com/e2b-dev/code-interpreter.git
```

在code-interpreter/template下创建Dockerfile：

```dockerfile
FROM python:3.12
USER root
WORKDIR /root
ENV JAVA_VERSION=11
ENV PIP_DEFAULT_TIMEOUT=100 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 JUPYTER_CONFIG_PATH=/root/.jupyter IPYTHON_CONFIG_PATH=/root/.ipython SERVER_PATH=/root/.server JAVA_HOME=/usr/lib/jvm/jdk-${JAVA_VERSION} IJAVA_VERSION=1.3.0 DENO_INSTALL=/opt/deno DENO_VERSION=v2.4.0 R_VERSION=4.5.*
RUN apt-get update && DEBIAN_FRONTEND=noninteractive DEBCONF_NOWARNINGS=yes apt-get install -y build-essential curl git util-linux jq sudo fonts-noto-cjk ca-certificates
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
RUN apt-get update && DEBIAN_FRONTEND=noninteractive DEBCONF_NOWARNINGS=yes apt-get install -y nodejs
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
RUN ipython kernel install --name 'python3' --user
RUN npm install -g --unsafe-perm git+https://github.com/e2b-dev/ijavascript.git
RUN ijsinstall --install=global
COPY server .server
RUN python -m venv .server/.venv
RUN .server/.venv/bin/pip install --no-cache-dir -r .server/requirements.txt
COPY matplotlibrc .config/matplotlib/.matplotlibrc
COPY start-up.sh .jupyter/start-up.sh
RUN chmod +x .jupyter/start-up.sh
COPY jupyter_server_config.py .jupyter/
RUN mkdir -p .ipython/profile_default/startup
COPY ipython_kernel_config.py .ipython/profile_default/
COPY startup_scripts .ipython/profile_default/startup
RUN apt-get -q update && \
    DEBIAN_FRONTEND=noninteractive DEBCONF_NOWARNINGS=yes \
    apt-get -qq -o=Dpkg::Use-Pty=0 install -y --no-install-recommends \
    systemd systemd-sysv openssh-server sudo chrony linuxptp socat curl
RUN useradd -m user
RUN mkdir -p /home/user
RUN chown -R user:user /home/user
RUN echo 'user ALL=(ALL) NOPASSWD: ALL' >> /etc/sudoers
USER user
WORKDIR /home/user
ENTRYPOINT ["sudo", "/root/.jupyter/start-up.sh"]
```

构建镜像：

```bash
docker build -t code-interpreter:v1 .
```

## 验证说明

能够正常创建模板并创建沙箱即可
