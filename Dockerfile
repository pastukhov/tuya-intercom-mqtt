FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[mqtt]" && useradd --uid 10001 --create-home capture
USER 10001:10001
ENTRYPOINT ["python", "-m", "domofon_capture"]
