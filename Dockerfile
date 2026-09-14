FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY collector ./collector
COPY config ./config
COPY docs/data ./docs/data
CMD ["sh", "-c", "python -m collector.main && python -m collector.publish"]
