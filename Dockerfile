FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir ".[mqtt]" && useradd --uid 10001 --create-home capture
USER 10001:10001
ENTRYPOINT ["python", "-m", "domofon_capture"]
