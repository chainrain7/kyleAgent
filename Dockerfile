# kyleAgent 容器镜像:daemon(kyle-core)与 Web Console 桥接器(kyle-webui)共用,
# docker compose 双服务基于本镜像(见 docker-compose.yml)
# 构建：docker build -t kyle-agent .
# 运行：docker run --rm -p 7437:7437 -e ANTHROPIC_API_KEY=sk-ant-... kyle-agent
# 或一键双服务：docker compose up --build   →  浏览器 http://localhost:8080
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# 先装依赖（利用层缓存），再拷源码
COPY pyproject.toml uv.lock ./
COPY src ./src
# --extra webui:装齐桥接器依赖(fastapi/uvicorn/websockets),compose 双服务共用本镜像
RUN uv sync --frozen --no-dev --extra webui

EXPOSE 7437

# 容器内无 TTY 交互，日志直接输出到 stdout/stderr
ENV KYLE_LOG_FILE=""
ENTRYPOINT ["uv", "run", "kyle-core"]
