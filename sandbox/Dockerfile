# 此镜像由用户在任务开始前显式构建；V3 执行器不构建或拉取镜像。
#在 AICodingDoctor 项目根目录运行：
#docker build -t aicd-pytest:local ./sandbox
FROM python:3.13-slim
RUN python -m pip install --no-cache-dir 'pytest>=8,<10'
ENV PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
USER 65534:65534
WORKDIR /workspace
