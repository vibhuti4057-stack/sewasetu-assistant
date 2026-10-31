FROM python:3.11-slim
WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "mcp>=1.12,<2" uvicorn
COPY app/ .
COPY assistant/server.py .
COPY tools/ /app/tools/
EXPOSE 8001
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8001", "--proxy-headers", "--forwarded-allow-ips", "*"]
