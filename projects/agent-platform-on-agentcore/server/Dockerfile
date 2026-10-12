FROM python:3.13-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
# --timeout-graceful-shutdown: uvicorn sends the lifespan shutdown (which cancels
# in-flight chat runs so their threads end `interrupted`, see run_broker.py) only
# after open connections close. An SSE subscriber never closes on its own, and
# ECS SIGKILLs after 30s — so without a bound a watched run would leave every
# run stuck on `busy`.
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "10"]
