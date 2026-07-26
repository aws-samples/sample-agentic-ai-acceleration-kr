"""
setup_anomaly_alarms.py — agentops-kit infra/anomaly_setup.py 를 이 runtime의
genai.* 메트릭(observability.py 가 기록)에 맞게 각색한 CloudWatch 이상탐지 설정.

메트릭별로 ML 기반 이상탐지 모델(put_anomaly_detector)을 등록하고,
ANOMALY_DETECTION_BAND 밴드를 벗어나면 울리는 알람(put_metric_alarm)을 만듭니다.
이상탐지 모델은 등록 후 실제 트래픽 데이터가 수 시간~수 일 쌓여야 밴드가 안정화됩니다.

사용:
    python setup_anomaly_alarms.py                              # 알람 생성/갱신
    python setup_anomaly_alarms.py --sns-topic-arn arn:aws:sns:...  # 알림 연동
    python setup_anomaly_alarms.py --delete                     # 정리
"""
import argparse
import os

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
NAMESPACE = os.environ.get("GENAI_METRICS_NAMESPACE", "bedrock-agentcore")
AGENT_NAME = os.environ.get("GENAI_AGENT_NAME", "anycompany_ecommerce")
DIMENSIONS = [{"Name": "Agent", "Value": AGENT_NAME}]

# (메트릭, 통계, 밴드 폭, 비교 연산자) — agentops-kit 과 동일한 5분/3평가/2트리거 정책.
# 레이턴시·토큰은 양방향(급증·급감 모두 이상), 에러·비용은 상방만 감시합니다.
MONITORED = [
    ("genai.invocation.latency", "Average", 2, "LessThanLowerOrGreaterThanUpperThreshold"),
    ("genai.token.input", "Average", 3, "LessThanLowerOrGreaterThanUpperThreshold"),
    ("genai.error.count", "Sum", 2, "GreaterThanUpperThreshold"),
    ("genai.cost.usd", "Sum", 3, "GreaterThanUpperThreshold"),
]


def alarm_name(metric: str) -> str:
    return f"{AGENT_NAME}-{metric}-anomaly"


def setup(sns_topic_arn: str | None):
    cw = boto3.client("cloudwatch", region_name=REGION)
    for metric, stat, band, operator in MONITORED:
        cw.put_anomaly_detector(SingleMetricAnomalyDetector={
            "Namespace": NAMESPACE, "MetricName": metric,
            "Dimensions": DIMENSIONS, "Stat": stat})
        cw.put_metric_alarm(
            AlarmName=alarm_name(metric),
            AlarmDescription=f"{AGENT_NAME} {metric} 이상탐지 (band={band})",
            Metrics=[
                {"Id": "m1", "ReturnData": True,
                 "MetricStat": {"Metric": {"Namespace": NAMESPACE, "MetricName": metric,
                                           "Dimensions": DIMENSIONS},
                                "Period": 300, "Stat": stat}},
                {"Id": "ad1", "ReturnData": True,
                 "Expression": f"ANOMALY_DETECTION_BAND(m1, {band})",
                 "Label": f"{metric} (expected)"},
            ],
            ThresholdMetricId="ad1",
            ComparisonOperator=operator,
            EvaluationPeriods=3,
            DatapointsToAlarm=2,
            TreatMissingData="notBreaching",
            ActionsEnabled=bool(sns_topic_arn),
            AlarmActions=[sns_topic_arn] if sns_topic_arn else [],
        )
        print(f"  ok: {alarm_name(metric)} ({stat}, band={band})")
    print(f"\n{len(MONITORED)}개 이상탐지 알람 구성 완료 "
          f"(namespace={NAMESPACE}, region={REGION})")
    if not sns_topic_arn:
        print("알림을 받으려면 --sns-topic-arn 으로 다시 실행하세요.")


def delete():
    cw = boto3.client("cloudwatch", region_name=REGION)
    cw.delete_alarms(AlarmNames=[alarm_name(m) for m, *_ in MONITORED])
    for metric, stat, _, _ in MONITORED:
        try:
            cw.delete_anomaly_detector(SingleMetricAnomalyDetector={
                "Namespace": NAMESPACE, "MetricName": metric,
                "Dimensions": DIMENSIONS, "Stat": stat})
        except cw.exceptions.ResourceNotFoundException:
            pass
    print("이상탐지 알람/모델 삭제 완료")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sns-topic-arn", help="알람 발생 시 알림을 보낼 SNS 토픽 ARN")
    parser.add_argument("--delete", action="store_true", help="알람/이상탐지 모델 삭제")
    args = parser.parse_args()
    delete() if args.delete else setup(args.sns_topic_arn)
