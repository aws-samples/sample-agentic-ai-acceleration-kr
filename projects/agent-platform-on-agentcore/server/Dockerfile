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
#
# --timeout-keep-alive: the web proxy (Node's fetch, undici) keeps connections to
# this server open and reuses them. uvicorn's 5 s default closed them from this
# side while the proxy was about to send on them, and under load the proxy's
# fetch failed with "other side closed" — a 500 to the browser for a request
# that never reached here (bst load test, 2026-10-18). The server must outlive
# the client's idle timeout, so the client is always the one that closes.
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "10", "--timeout-keep-alive", "65"]
