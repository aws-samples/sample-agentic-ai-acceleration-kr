# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""본문 로깅 sink — **꺼진 상태가 기본값인지**, 그리고 켤 때의 안전장치가 붙어 있는지.

무엇을 지키려는 것인가
----------------------
이 sink 에 담기는 것은 마스킹되지 않은 프롬프트/응답 본문이다. 그래서 실수의 비용이
비대칭이다 — 실수로 켜지면 사용자 데이터가 durable 저장소에 남고 되돌릴 수 없다.
그 방향의 실수를 막는 것들은 전부 **기본값과 게이팅**이고, 기본값은 코드를 읽어서는
어긋난 것을 알아채기 어렵다(잘못 켜져 있어도 아무 오류가 나지 않는다).

여기서 세 층을 고정한다:

  1. terraform 모듈 — `enabled=false` 면 리소스가 **하나도** 만들어지지 않는지.
     count 게이팅을 한 리소스에서만 빼먹으면 그 리소스만 조용히 생성된다.
  2. helm 차트 — `FIREHOSE_STREAM_NAME` 이 기본 비어 있고, 실제로 컨테이너까지
     전달되는지. 전달되지 않으면 운영자가 값을 넣어도 로거는 계속 no-op 이다.
  3. IAM — 게이트웨이에 주는 권한이 **쓰기 전용**인지. 읽기 권한이 있으면 파드 침해가
     곧 누적된 전체 프롬프트 이력의 유출이 된다.

⚠️ HCL 을 정규식으로 읽는다. 파서를 붙이지 않는 이유는 여기서 확인하려는 것이
   구문 구조가 아니라 "특정 문자열이 특정 블록 안에 있는가" 라는 국소적 성질이고,
   각 단정에 **대조군**(그 문자열을 지우면 실패하는지)이 붙어 있기 때문이다.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="pyyaml 이 필요하다(렌더 결과 파싱)")

CHART = Path(__file__).resolve().parents[1]
TF = CHART.parents[1] / "terraform"
MODULE = TF / "modules" / "body-logging"


def _read(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    # 대조군 — 경로 오타를 "일치" 로 오판하지 않는다.
    assert len(text) > 400, f"{path} 가 너무 짧다 — 경로가 틀렸을 것이다"
    return text


def _blocks(hcl: str, kind: str) -> dict[str, str]:
    """최상위 `kind "a" "b" { ... }` 블록들을 이름→본문으로. 중괄호 깊이로 자른다."""
    out: dict[str, str] = {}
    pattern = re.compile(rf'^{kind}\s+("(?:[^"]+)"(?:\s+"[^"]+")?)\s*\{{', re.M)
    for m in pattern.finditer(hcl):
        name = " ".join(p.strip('"') for p in m.group(1).split())
        depth, i = 1, m.end()
        while i < len(hcl) and depth:
            if hcl[i] == "{":
                depth += 1
            elif hcl[i] == "}":
                depth -= 1
            i += 1
        out[name] = hcl[m.end() : i - 1]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 1. terraform 모듈 — 꺼져 있으면 아무것도 만들지 않는다
# ─────────────────────────────────────────────────────────────────────────────


def test_local_count_is_the_enabled_gate():
    main = _read(MODULE / "main.tf")
    assert re.search(r"count\s*=\s*var\.enabled\s*\?\s*1\s*:\s*0", main), (
        "locals 의 count 게이트를 찾지 못했다 — 이 파일의 나머지 검사가 공허해진다"
    )


def test_every_resource_is_gated_on_enabled():
    """⚠️ 이 파일의 핵심 단정.

    리소스 하나에서 count 를 빼먹으면 `enabled=false` 인 배포에서도 그것만 만들어진다.
    S3 버킷이라면 마스킹되지 않은 본문을 받을 준비가 된 버킷이 아무도 모르게 존재한다.
    """
    main = _read(MODULE / "main.tf")
    resources = _blocks(main, "resource")
    assert len(resources) >= 7, f"리소스가 {len(resources)}개뿐이다 — 블록 파싱을 확인"

    ungated = []
    for name, body in resources.items():
        # `count = local.count` 또는 `count = var.enabled && ... ? 1 : 0` 둘 다 허용.
        m = re.search(r"^\s*count\s*=\s*(.+)$", body, re.M)
        if m is None or ("local.count" not in m.group(1) and "var.enabled" not in m.group(1)):
            ungated.append(name)
    assert ungated == [], (
        f"count 게이팅이 없는 리소스: {ungated} — enabled=false 인데도 만들어진다"
    )


def test_outputs_do_not_error_when_disabled():
    """비활성 시 output 이 인덱스 참조로 죽으면 스택 전체 plan 이 실패한다."""
    outputs = _blocks(_read(MODULE / "outputs.tf"), "output")
    assert len(outputs) == 4, f"output 이 {len(outputs)}개 — 4 를 기대"
    for name, body in outputs.items():
        assert "try(" in body, f"output {name} 이 try() 로 감싸이지 않았다"
        assert re.search(r',\s*""\s*\)', body), (
            f"output {name} 의 폴백이 빈 문자열이 아니다 — gateway-proxy 의 "
            "'미설정 = no-op' 판정이 빈 문자열에 달려 있다"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. 기본값 — 안전한 쪽
# ─────────────────────────────────────────────────────────────────────────────


def _variable_default(hcl: str, name: str) -> str:
    body = _blocks(hcl, "variable")[name]
    m = re.search(r"^\s*default\s*=\s*(.+)$", body, re.M)
    assert m is not None, f"variable {name} 에 default 가 없다"
    return m.group(1).strip()


@pytest.mark.parametrize("var_name", ["enabled", "force_destroy"])
def test_dangerous_switches_default_off(var_name: str):
    vars_hcl = _read(MODULE / "variables.tf")
    assert _variable_default(vars_hcl, var_name) == "false", (
        f"{var_name} 의 기본값이 false 가 아니다 — 배포만으로 위험한 상태가 된다"
    )


def test_retention_defaults_to_a_real_expiry():
    """⚠️ 원본 구현은 0(무기한)이 기본이었다.

    마스킹되지 않은 프롬프트 본문을 만료 없이 쌓는 것은 켜는 순간부터 조용히 늘어나는
    부채다. 0 은 지정할 수 있어야 하지만 **기본값**이면 안 된다.
    """
    default = _variable_default(_read(MODULE / "variables.tf"), "log_retention_days")
    assert int(default) > 0, f"log_retention_days 기본값이 {default} — 만료 없음이 기본이다"


def test_lifecycle_aborts_incomplete_multipart_uploads():
    """빼면 실패한 멀티파트 업로드의 파트가 **영구히** 남는다.

    expiration 은 완성된 객체만 지운다. 큰 본문은 멀티파트로 올라오므로, 이 블록이
    없으면 "N일 뒤 삭제" 라고 믿는 데이터의 일부가 계속 남고 계속 과금된다.
    """
    main = _read(MODULE / "main.tf")
    lifecycle = _blocks(main, "resource")["aws_s3_bucket_lifecycle_configuration body_logs"]
    assert "abort_incomplete_multipart_upload" in lifecycle


def test_versioning_is_disabled_so_expiration_actually_deletes():
    """버저닝이 켜져 있으면 expiration 은 현재 버전만 만료시킨다.

    비현재 버전은 `noncurrent_version_expiration` 없이는 남으므로, retention 변수가
    약속하는 것과 실제가 어긋난다 — 지워졌다고 믿는 본문이 남아 있는 상태다.
    """
    main = _read(MODULE / "main.tf")
    versioning = _blocks(main, "resource")["aws_s3_bucket_versioning body_logs"]
    assert re.search(r'status\s*=\s*"Disabled"', versioning), (
        "버저닝이 Disabled 가 아니다 — noncurrent_version_expiration 도 함께 걸어야 한다"
    )


def test_bucket_is_ownership_enforced_and_not_public():
    main = _read(MODULE / "main.tf")
    resources = _blocks(main, "resource")
    assert "aws_s3_bucket_ownership_controls body_logs" in resources, (
        "ownership controls 가 없다 — public_access_block 은 *public* 만 막고 "
        "특정 계정에 ACL 로 읽기를 주는 경로는 열려 있다"
    )
    assert re.search(
        r'object_ownership\s*=\s*"BucketOwnerEnforced"',
        resources["aws_s3_bucket_ownership_controls body_logs"],
    )
    pab = resources["aws_s3_bucket_public_access_block body_logs"]
    for key in (
        "block_public_acls",
        "block_public_policy",
        "ignore_public_acls",
        "restrict_public_buckets",
    ):
        assert re.search(rf"{key}\s*=\s*true", pab), f"{key} 가 true 가 아니다"


def test_bucket_name_is_structurally_distinct_from_the_native_sink():
    """⚠️ 이름이 한 단어 차이면 바꿔 넣어도 오류가 나지 않는다.

    대조 도구는 두 버킷을 모두 인자로 받는다(게이트웨이 자체 본문 / AWS 네이티브
    invocation log). 틀리면 Athena 테이블은 정상 생성되고 스캔만 0건이 되어, "데이터가
    없다" 와 "버킷을 잘못 짚었다" 를 구분할 수 없다. 원본 구현이 정확히 그 상태였다.
    """
    ours = _read(MODULE / "main.tf")
    native = _read(TF / "modules" / "bedrock-invocation-logging" / "main.tf")

    def bucket_expr(hcl: str) -> str:
        # ⚠️ 주석을 먼저 지운다. 두 모듈 모두 주석 안에서 서로의 버킷 이름을 인용하므로,
        #    안 지우면 인용된 이름을 실제 값으로 읽어 항상 충돌로 판정한다(또는 그 반대).
        code = re.sub(r"#[^\n]*", "", hcl)
        m = re.search(r"^\s*bucket_name\s*=\s*(.+)$", code, re.M)
        assert m is not None, "bucket_name 지역값을 찾지 못했다"
        return m.group(1)

    ours_name, native_name = bucket_expr(ours), bucket_expr(native)
    # 리터럴 토큰(하이픈 구분)만 비교한다 — 보간 부분은 두 모듈이 같아도 된다.
    def tokens(expr: str) -> set[str]:
        literals = re.sub(r"\$\{[^}]*\}", " ", expr)
        return {t for t in re.split(r"[^a-z0-9]+", literals.lower()) if len(t) > 3}

    shared = tokens(ours_name) & tokens(native_name)
    assert shared == set(), (
        f"두 버킷 이름이 리터럴 토큰 {sorted(shared)} 을 공유한다 — 서로 바꿔 넣는 "
        "실수가 오류 없이 통과한다. 구조적으로 다른 이름을 쓸 것"
    )


def test_buffer_minimums_are_validated_at_plan_time():
    """동적 파티셔닝은 64MB/60s 하한이 있고, 위반은 **apply 중에** 실패한다.

    plan 이 통과하고 apply 가 절반 진행된 뒤 깨지는 것이 가장 비싼 실패다.
    """
    vars_hcl = _read(MODULE / "variables.tf")
    for name in ("buffer_size_mb", "buffer_interval_seconds"):
        body = _blocks(vars_hcl, "variable")[name]
        assert "validation" in body, f"{name} 에 validation 이 없다"


# ─────────────────────────────────────────────────────────────────────────────
# 3. IAM — 쓰기 전용
# ─────────────────────────────────────────────────────────────────────────────

_FORBIDDEN_ON_BODY_LOGS = (
    "s3:GetObject",
    "s3:ListBucket",
    "s3:DeleteObject",
    "firehose:DescribeDeliveryStream",
    "firehose:ListDeliveryStreams",
)


def _actions_of(hcl: str, sid: str) -> list[str]:
    """해당 sid statement 의 `actions = [...]` 목록.

    ⚠️ 주석을 먼저 지운다. 예전에 이 검사가 **자기 근거를 자기 주석에서 읽어** 실패한
       적이 있다("s3:ListBucket 은 주지 않는다" 라는 주석이 위반으로 잡혔다).
       테스트는 실행되는 것만 봐야 한다.
    """
    code = re.sub(r"#[^\n]*", "", hcl)
    idx = code.index(f'sid       = "{sid}"') if f'sid       = "{sid}"' in code else code.index(sid)
    m = re.search(r"actions\s*=\s*\[(.*?)\]", code[idx:], re.S)
    assert m is not None, f"{sid} 의 actions 목록을 찾지 못했다"
    return re.findall(r'"([^"]+)"', m.group(1))


def test_gateway_proxy_gets_write_only_access():
    """게이트웨이는 자기가 넣은 본문을 다시 읽을 이유가 없다.

    읽기 권한이 있으면 파드 침해가 곧 **누적된 전체 프롬프트 이력의 유출**이 된다.
    쓰기만 주면 침해로 얻는 것은 "앞으로 들어올 것" 뿐이다.
    """
    irsa = _read(TF / "modules" / "irsa" / "main.tf")
    allowed = {
        "BodyLogFirehoseWrite": {"firehose:PutRecord", "firehose:PutRecordBatch"},
        "BodyLogS3Fallback": {"s3:PutObject"},
    }
    for sid, expected in allowed.items():
        actions = set(_actions_of(irsa, sid))
        assert actions, f"{sid} 의 actions 가 비었다 — 파싱을 확인"
        # 허용목록 방식이다. 금지목록으로 하면 목록에 없는 새 읽기 액션이 통과한다.
        extra = actions - expected
        assert extra == set(), f"{sid} 에 쓰기 외 권한이 있다: {sorted(extra)}"
        for action in _FORBIDDEN_ON_BODY_LOGS:
            assert action not in actions, f"{sid} 에 읽기/삭제 권한 {action} 이 있다"


def test_body_log_statements_are_gated_on_a_non_empty_arn():
    """빈 ARN 으로 렌더되면 `resources = [""]` 가 되어 apply 가
    MalformedPolicyDocument 로 깨진다 — 로깅을 안 쓰는 배포 전체가 막힌다.
    """
    irsa = _read(TF / "modules" / "irsa" / "main.tf")
    for var_name in ("body_log_firehose_arn", "body_log_bucket_arn"):
        assert re.search(rf'var\.{var_name}\s*!=\s*""\s*\?\s*\[1\]\s*:\s*\[\]', irsa), (
            f"{var_name} 의 non-empty 게이트를 찾지 못했다"
        )


def test_s3_fallback_is_scoped_to_objects_not_the_bucket():
    irsa = _read(TF / "modules" / "irsa" / "main.tf")
    idx = irsa.index("BodyLogS3Fallback")
    chunk = irsa[idx : idx + 400]
    assert 'var.body_log_bucket_arn}/*' in chunk, (
        "s3:PutObject 대상이 객체 경로(/*)로 좁혀지지 않았다"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. 환경 배선 — 모듈이 실제로 호출되고 IRSA 로 연결되는지
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("env_name", ["llm-gateway-dev", "llm-gateway-prod"])
def test_environment_wires_the_module_and_the_irsa_arns(env_name: str):
    """모듈만 만들고 IRSA 에 연결하지 않으면 게이트웨이가 403 으로 조용히 실패한다.

    Firehose Put 실패는 로거가 삼키므로(요청을 깨뜨리지 않는 것이 옳다) 증상이 없다 —
    토글은 ON 이고 화면은 정상이고 S3 는 비어 있다.
    """
    main = _read(TF / "environments" / env_name / "main.tf")
    assert 'module "body_logging"' in main, f"{env_name} 이 모듈을 호출하지 않는다"
    assert "body_log_firehose_arn = module.body_logging.firehose_stream_arn" in main, (
        f"{env_name}: IRSA 에 Firehose ARN 을 넘기지 않는다 — 게이트웨이가 쓰지 못한다"
    )
    assert "body_log_bucket_arn   = module.body_logging.bucket_arn" in main, (
        f"{env_name}: IRSA 에 버킷 ARN 을 넘기지 않는다"
    )


@pytest.mark.parametrize("env_name", ["llm-gateway-dev", "llm-gateway-prod"])
def test_environment_defaults_the_switch_off(env_name: str):
    vars_hcl = _read(TF / "environments" / env_name / "variables.tf")
    assert _variable_default(vars_hcl, "enable_body_logging") == "false"


@pytest.mark.parametrize("env_name", ["llm-gateway-dev", "llm-gateway-prod"])
def test_environment_exposes_the_two_helm_values(env_name: str):
    """운영자가 helm 에 옮길 값이 output 으로 나와 있어야 한다.

    없으면 스트림 이름을 콘솔에서 찾아 손으로 적게 되고, 그러면 오타가 "로깅이 켜진
    것처럼 보이지만 아무것도 안 쌓이는" 상태로 나타난다.
    """
    outputs = _blocks(_read(TF / "environments" / env_name / "outputs.tf"), "output")
    assert "body_log_firehose_stream" in outputs
    assert "body_log_bucket" in outputs


# ─────────────────────────────────────────────────────────────────────────────
# 5. helm — 기본 OFF 이고, 값이 실제로 컨테이너까지 간다
# ─────────────────────────────────────────────────────────────────────────────


def _helm() -> str:
    exe = shutil.which("helm")
    if exe is None:
        if os.environ.get("CI"):
            raise AssertionError(
                "CI 인데 helm 이 없다 — 렌더 테스트가 조용히 skip 되면 본문 로깅의 "
                "'기본 OFF' 가 아무 데서도 검증되지 않는다."
            )
        pytest.skip("helm 바이너리가 없다")
    return exe


def _gateway_env(*set_args: str) -> dict[str, str | None]:
    cmd = [_helm(), "template", "bodylog-test", str(CHART)]
    for a in set_args:
        cmd += ["--set", a]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"helm template 실패:\n{proc.stderr[-2000:]}"
    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    assert len(docs) > 5, f"렌더 결과가 {len(docs)}개뿐이다"
    for d in docs:
        if d.get("kind") == "Deployment" and "gateway-proxy" in d["metadata"]["name"]:
            return {
                e["name"]: e.get("value")
                for e in d["spec"]["template"]["spec"]["containers"][0].get("env", [])
            }
    raise AssertionError("gateway-proxy Deployment 를 렌더 결과에서 찾지 못했다")


def test_stream_name_is_empty_by_default():
    """⚠️ 이것이 정적 잠금이다. 비어 있으면 로거는 boto3 클라이언트조차 만들지 않는다."""
    env = _gateway_env()
    assert env.get("FIREHOSE_STREAM_NAME") == "", (
        f"기본 FIREHOSE_STREAM_NAME 이 {env.get('FIREHOSE_STREAM_NAME')!r} — "
        "배포만으로 본문 수집 인프라가 활성화된다"
    )
    assert env.get("BODY_LOG_S3_BUCKET") == ""


def test_operator_supplied_values_reach_the_container():
    """대조군 — 위 단정이 "이 키가 아예 없다" 를 통과시키는 것이 아님을 보인다.

    키가 컨테이너까지 가지 않으면 운영자가 값을 넣어도 로거는 계속 no-op 이고,
    화면상 원인이 보이지 않는다.
    """
    env = _gateway_env(
        "gatewayProxy.env.FIREHOSE_STREAM_NAME=example-body-logs",
        "gatewayProxy.env.BODY_LOG_S3_BUCKET=example-bucket",
    )
    assert env.get("FIREHOSE_STREAM_NAME") == "example-body-logs"
    assert env.get("BODY_LOG_S3_BUCKET") == "example-bucket"
