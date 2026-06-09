# e2b-infra

## Introduction

[E2B](https://e2b.dev/ ) is an open-source infrastructure for AI code interpretation.
In our main repository [e2b-dev/e2b](https://github.com/e2b-dev/E2B ) we provide SDKs and a CLI that let you customize and manage environments and run your AI agents in the cloud.

This repository contains the low-level infrastructure code that powers the E2B platform.



## Installation Guide

### Prerequisites

- Self-hosted PostgreSQL (≥ v14)
- Self-hosted Harbor registry
- Self-hosted MinIO
- At least 4 nodes forming 4 clusters: api (edge), build (template-builder), default (sandbox workload) and nomad-server
- Only `e2b-2.9.0` and `e2b_code_interpreter-2.4.1` are supported—use them together
- Host kernel & BIOS must have virtualization enabled
- runc ≥ 1.0.2
- Docker ≥ 25.0.3

1. Plan the clusters and install this package on every node.
   Deployment tools will be placed under `/opt/e2b-infra`:

   ```
   ├── bin                 # binaries
   ├── deploy.sh           # nomad service deploy script
   ├── env.template        # env template
   ├── init-client.sh      # init client node
   ├── install-consul.sh
   ├── install-nomad.sh
   ├── nomad
   ├── nomad.service
   ├── run-consul.sh
   ├── run-nomad.sh
   ├── start-api.sh
   ├── start-client.sh     # start client node
   ├── start-server.sh     # start server node
   ├── uninstall-consul.sh
   └── uninstall-nomad.sh
   ```

   

2. On server nodes copy the template and fill in custom variables:`cp env.template .env`

   ```
   export SERVER_IPS="xxx xxx"                        # space-separated IPs of nomad-server nodes
   export NUM_SERVERS=1                               # number of server nodes
   export REGISTRY_URL=xxx:2900/e2b-orchestration     # Harbor URL
   export POSTGRES_CONNECTION_STRING="xxx"            # postgres URI
   export HARBOR_HOST=xxx                             # harbor host
   export MINIO_ENDPOINT="61.47.17.181:9000"          # minio endpoint
   export MINIO_ACCESS_KEY="minioadmin"               # minio access key
   export MINIO_SECRET_KEY="minioadmin"               # minio secret
   
   ```

   

3. On every server node run:
   `bash start-server.sh {IP-to-use}`
   This installs/configures consul & nomad, creates ACL tokens and writes them into `.env`:

   ```
   export CONSUL_ACL_TOKEN=xxx
   export NOMAD_ACL_TOKEN=xxx
   ```

4. (Optional) For additional nomad-server nodes, copy `.env` to each and repeat step 3; they will auto-join.

5. Copy `.env` to every client node and run:
   `bash start-client.sh {node_pool} {IP-to-use}`
   where node_pool is one of: api / default / build.
   Clients install consul & nomad and join the cluster.

6. On build/default nodes run `bash init-client.sh` to auto-configure sandbox services.

7. When the cluster is up, run `bash deploy.sh` on a server node.
   It pulls images, repackages them, starts all supporting services and injects the initial user.

8. Visit `http://{server_ip}:4646/ui` to inspect nomad status.

   

## Interacting with the Cluster

### Install the CLI

Install Python dependencies
(only `e2b-2.9.0` & `e2b_code_interpreter-2.4.1` are supported):

```bash
pip install e2b==2.9.0
pip install e2b_code_interpreter==2.4.1
```

##### Export cluster environment:

```sh
export E2B_ACCESS_TOKEN="sk_e2b_xxx"
export E2B_API_KEY="e2b_xxx"
export E2B_DOMAIN="xxx"                    # domain pointing to client-proxy:3002
export E2B_API_URL="http://171.1.3.3:3000"
export E2B_HTTP_SSL="false"
```

##### Declare a Template

1. Base sandbox (shell only):

   ```
   # template.py
   from e2b import Template
   
   template = (
       Template()
       .from_dockerfile('FROM harbor:443/e2b-orchestration/ubuntu:22.04'))
   ```

2. Code-interpreter sandbox (auto-runs Python; image built below):

   ```
   # template.py
   from e2b_code_interpreter import Template, wait_for_url
   
   template = (
       Template()
       .from_dockerfile('FROM harbor:443/e2b-orchestration/code-interpreter:v1')
       .set_start_cmd(
           "sudo /root/.jupyter/start-up.sh", wait_for_url("http://localhost:49999/health")
       ))
   ```

   

   

   ##### Build the Template

   ```python
   # build_prod.py
   import os
   
   os.environ["E2B_ACCESS_TOKEN"] = "sk_e2b_..."
   os.environ["E2B_API_KEY"]  = "e2b_..."
   os.environ["E2B_API_URL"]  = "http://171.1.3.3:3000"
   os.environ["E2B_HTTP_SSL"] = "false"
   
   from e2b import Template, default_build_logger
   from template import template
   
   if __name__ == '__main__':
       Template.build(
           template,
           alias="code-interpreter-v1",
           cpu_count=1,
           memory_mb=1024,
           on_build_logs=default_build_logger()
       )
   ```

   ##### Start a Sandbox

   ```python
   import os
   os.environ["E2B_ACCESS_TOKEN"] = "sk_e2b_..."
   os.environ["E2B_API_KEY"]  = "e2b_..."
   os.environ["E2B_DOMAIN"]  = "e2b.app"
   os.environ["E2B_API_URL"]  = "http://171.1.3.3:3000"
   os.environ["E2B_HTTP_SSL"] = "false"
   
   from e2b import Sandbox
   
   sbx = Sandbox.create("basev61")
   print(sbx.commands.run("whoami"))  # guest
   print(sbx.commands.run("pwd"))     # /home/guest
   sbx.kill()
   ```

   Code sandbox:

   ```python
   import os
   # same env as above
   from e2b_code_interpreter import Sandbox
   
   sbx = Sandbox.create("code-interpreter-v9")
   execution = sbx.run_code('print("Hello, world!")')
   print(execution)
   sbx.kill()
   ```

   ##### Build the Template Image

   Clone the official template:

   ```bash
   git clone https://github.com/e2b-dev/code-interpreter.git
   ```

   Create Dockerfile (content identical to the Chinese section).

   Build:

   ```bash
   docker build -t code-interpreter:v1 .
   ```

## Contributing

1. Fork this repository
2. Create a feature branch `Feat_xxx`
3. Commit your changes
4. Open a Pull Request

## Misc

1. Use `Readme_XXX.md` for multi-language support, e.g. `Readme_en.md`, `Readme_zh.md`
2. Gitee official blog: [blog.gitee.com](https://blog.gitee.com/ )
3. Explore excellent open-source projects on Gitee: https://gitee.com/explore 
4. [GVP](https://gitee.com/gvp ) – Gitee’s Most Valuable Open-Source Project award
5. Gitee official help: https://gitee.com/help 
6. Gitee Stars showcase: https://gitee.com/gitee-stars/
