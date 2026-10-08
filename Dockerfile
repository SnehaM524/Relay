FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY relay ./relay
COPY config ./config
RUN pip install --no-cache-dir .
ENV RELAY_CONFIG_DIR=/app/config
EXPOSE 8080
CMD ["relay", "serve", "--host", "0.0.0.0", "--port", "8080"]
