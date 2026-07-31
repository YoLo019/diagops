#!/bin/sh
set -eu

# range 适配器的 baseline 是诊断窗口（30m）前等长的 60m 窗口。
# seed 覆盖 [now-100m, now-31m]，步长 240s（< 300s），保证任意 rate[5m]
# 在 baseline 窗口内至少有两个采样点。
DIAGOPS_SEED_OLDEST_SECONDS=6000
DIAGOPS_SEED_NEWEST_SECONDS=1900
DIAGOPS_SEED_STEP_SECONDS=240

now=$(date +%s)
baseline_file=/tmp/diagops-baseline.prom
: > "$baseline_file"

cat >> "$baseline_file" <<EOF
# TYPE http_requests_total counter
# TYPE http_request_duration_seconds_bucket counter
# TYPE container_cpu_usage_seconds_total counter
# TYPE container_memory_usage_bytes gauge
# TYPE container_network_receive_packets_dropped_total counter
# TYPE kube_pod_container_status_restarts_total counter
EOF

i=0
for ago in $(seq $DIAGOPS_SEED_OLDEST_SECONDS -$DIAGOPS_SEED_STEP_SECONDS $DIAGOPS_SEED_NEWEST_SECONDS); do
  ts=$(( now - ago ))
  # baseline 速率 0.1/s：故障场景驱动的 24 个请求（rate[5m] 0.08/s）不构成
  # 正向 traffic anomaly，避免干扰根因 attribution；traffic_spike 场景的
  # 300 个请求（1/s）仍是 +900%。
  ok_requests=$(( 100 + 24 * i ))
  cpu=$(( 10 + i ))
  served=$(( 100 + i ))
  cat >> "$baseline_file" <<EOF
http_requests_total{service="checkout-service",environment="prod",status="200"} $ok_requests $ts
http_requests_total{service="checkout-service",environment="prod",status="500"} 0 $ts
http_requests_total{service="checkout-service",environment="prod",status="503"} 0 $ts
http_requests_total{service="checkout-service",environment="prod",status="504"} 0 $ts
http_request_duration_seconds_bucket{service="checkout-service",environment="prod",le="0.1"} $served $ts
http_request_duration_seconds_bucket{service="checkout-service",environment="prod",le="0.5"} $served $ts
http_request_duration_seconds_bucket{service="checkout-service",environment="prod",le="1.0"} $served $ts
http_request_duration_seconds_bucket{service="checkout-service",environment="prod",le="5.0"} $served $ts
http_request_duration_seconds_bucket{service="checkout-service",environment="prod",le="+Inf"} $served $ts
container_cpu_usage_seconds_total{container="checkout-service",environment="prod"} $cpu $ts
container_memory_usage_bytes{container="checkout-service",environment="prod"} 52428800 $ts
container_network_receive_packets_dropped_total{container="checkout-service",environment="prod"} 0 $ts
kube_pod_container_status_restarts_total{container="checkout-service",environment="prod"} 0 $ts
EOF
  i=$(( i + 1 ))
done

echo "# EOF" >> "$baseline_file"

/bin/promtool tsdb create-blocks-from openmetrics "$baseline_file" /prometheus
exec /bin/prometheus "$@"
