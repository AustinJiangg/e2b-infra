# build_prod.py
import os
import sys
from e2b import Template
os.environ["E2B_ACCESS_TOKEN"]="sk_e2b_17bd3933af21f80dc10bba686691c4fcd7057123"
os.environ["E2B_API_KEY"]="e2b_5ec17bd3933af21f80dc10bba686691c4fcd7057"
#os.environ["E2B_ACCESS_TOKEN"] = "sk_e2b_964e7687eae87a43f372b45b1c0144dbe6ac4696"
#os.environ["E2B_API_KEY"]  = "e2b_34c0a79abcda129f342333ae006c2ee9e263f9f8"
os.environ["E2B_API_URL"]="http://localhost:3000"
os.environ["E2B_HTTP_SSL"]="false"

from e2b import Template, default_build_logger
if __name__ == '__main__':
    name = sys.argv[1]
    Template.build(
        Template().from_dockerfile('FROM harbor:443/e2b-orchestration/ubuntu:22.04-custom'),
        alias=name,
        cpu_count=1,
        memory_mb=1024,
        on_build_logs=default_build_logger()
    )

#  Access Token: sk_e2b_7f6762a67ddbe33f0cdf17a6e4a1cd451a05be5d
#  Team API Key: e2b_f2aa1dca3ccfc4ce444725c1151fcd3f27d04bc2

