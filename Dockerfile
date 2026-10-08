FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY autopilot ./autopilot
COPY config ./config
COPY data ./data
RUN pip install --no-cache-dir .
ENTRYPOINT ["autopilot"]
CMD ["simulate"]
