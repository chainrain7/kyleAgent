# kyle-core daemon 容器镜像
# 构建：docker build -t kyle-agent .
# 运行：docker run --rm -p 7437:7437 -e ANTHROPIC_API_KEY=sk-ant-... kyle-agent
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# 先装依赖（利用层缓存），再拷源码
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev

EXPOSE 7437

# 容器内无 TTY 交互，日志直接输出到 stdout/stderr
ENV KYLE_LOG_FILE=""
ENTRYPOINT ["uv", "run", "kyle-core"]
