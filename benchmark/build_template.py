from dotenv import load_dotenv
load_dotenv()
from e2b import Template, default_build_logger

if __name__ == '__main__':
    Template.build(
        Template().from_dockerfile('FROM harbor:443/e2b-orchestration/ubuntu:22.04-custom'),
        alias="base",
        cpu_count=1,
        memory_mb=1024,
        on_build_logs=default_build_logger()
    )
