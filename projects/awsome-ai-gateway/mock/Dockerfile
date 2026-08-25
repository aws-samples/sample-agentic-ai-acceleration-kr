FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY ca-certificates.cr[t] /etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
COPY pyproject.toml ./
RUN uv pip install --system fastapi uvicorn
COPY main.py ./

# Run as a non-root user.
# EN: This mock vLLM image is for local docker-compose development only, but
#     we still drop root to match the rest of the stack and to keep the image
#     compatible with PodSecurity `restricted` if ever pushed to a cluster.
#     Port 8080 is unprivileged, so no extra capabilities are required.
# KO: 본 mock vLLM 이미지는 로컬 docker-compose 전용이지만, 다른 서비스와
#     일관성을 맞추고, 만일 클러스터에 배포되더라도 PodSecurity `restricted`
#     정책과 호환되도록 root 권한을 내려놓습니다. 포트 8080 은 비특권
#     포트라 추가 capability 가 필요하지 않습니다.
RUN groupadd --system --gid 10001 appgroup \
    && useradd  --system --uid 10001 --gid appgroup --create-home --home-dir /home/appuser appuser
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
