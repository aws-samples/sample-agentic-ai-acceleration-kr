# Load Test Dashboards

이 디렉토리에 Grafana dashboard JSON을 배치하면 sidecar 가 자동 import.

## Provisioning 방법

ConfigMap 형태로 감싸서 apply:

```bash
kubectl create configmap load-test-dashboard \
  -n observability \
  --from-file=load-test.json \
  --dry-run=client -o yaml | \
kubectl label --local -f - grafana_dashboard=1 --dry-run=client -o yaml | \
kubectl apply -f -
```

kube-prometheus-stack 의 grafana sidecar 가 label `grafana_dashboard=1` 붙은 ConfigMap 을 watch 하여 자동으로 Grafana 에 import.

## 대시보드 구조 (부하테스트용 11개 패널 권장)

1. Concurrent SSE connections — `gateway_proxy_active_connections`
2. Request rate by status — `sum by (status_code) (rate(gateway_proxy_request_total[1m]))`
3. TTFB p50/p95/p99 — `histogram_quantile(0.95, sum by (le) (rate(gateway_proxy_request_duration_bucket[1m])))`
4. Gateway Pod CPU — `sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="llm-gateway",pod=~"llm-gateway-gateway-proxy.*"}[1m]))`
5. Gateway Pod Memory — `container_memory_working_set_bytes{namespace="llm-gateway",pod=~"llm-gateway-gateway-proxy.*"}`
6. HPA replicas (desired/current) — `kube_horizontalpodautoscaler_status_desired_replicas` + `..._current_replicas`
7. 429 rate — `sum(rate(gateway_proxy_request_total{status_code="429"}[1m]))`
8. Rate limit hits by type — `sum by (scope, limit_type) (rate(gateway_proxy_rate_limit_hits_total[1m]))`
9. Token usage by model — `sum by (model) (rate(gateway_proxy_token_usage_total[1m]))`
10. Bedrock InvocationThrottles (CloudWatch) — AWS/Bedrock ModelId
11. Aurora DatabaseConnections (CloudWatch) — AWS/RDS DBClusterIdentifier

설치 후 Grafana UI (localhost:3000) 에서 "New dashboard" 로 위 쿼리들을 조립한 뒤
"Share → Export → Save JSON to file" 로 `load-test.json` 저장.
