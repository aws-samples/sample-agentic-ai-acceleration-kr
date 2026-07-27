"""Configuration Bundle 오버라이드 조회 (개선 파이프라인 Track A).

활성 bundle 은 SSM 파라미터(`CONFIG_BUNDLE_PARAM`, 기본값 없음 = 기능 비활성)가
단일 원천이다("bundle 승격 = SSM 포인터 전환"). 조회 흐름:

1. SSM `GetParameter` → JSON ``{"bundleId": "...", "versionId": "..."}``
2. bedrock-agentcore-control ``GetConfigurationBundleVersion(bundleId, versionId)``
3. ``components["orchestrator"]["configuration"]`` 에서 `system_prompt` / `model_id` 추출

components 의 키는 runtime ARN 대신 **논리 키 `"orchestrator"`** 를 쓴다
(자기 ARN 자기참조 회피 — 문서 예제와 의도적 편차).

**모든 실패는 경고 로그 + None 반환**(코드 기본값 폴백 — AGENTREL04). bundle 조회가
에이전트 가용성을 떨어뜨려서는 안 된다. microVM warm 재사용을 고려해 모듈 레벨
TTL 캐시(60초)를 둔다.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("orchestrator")

# 캐시 TTL(초). warm microVM 이 매 요청마다 SSM/control-plane 을 때리지 않게 한다.
CACHE_TTL_SECONDS = 60.0

# bundle components 의 논리 키.
COMPONENT_KEY = "orchestrator"

# 오버라이드가 없을 때의 bundle 라벨(version vector 스탬프용).
DEFAULT_BUNDLE_LABEL = "default"


@dataclass(frozen=True)
class BundleOverride:
    """활성 bundle 에서 읽은 오버라이드 값.

    - system_prompt: SYSTEM_PROMPT 를 대체할 프롬프트(없으면 None → 코드 기본값)
    - model_id: Settings.model_id 를 대체할 모델(없으면 None → 코드 기본값)
    - bundle_label: version vector 스탬프용 라벨. `"<bundleId>@<versionId>"`,
      미적용 시 `"default"`.
    """

    system_prompt: str | None = None
    model_id: str | None = None
    bundle_label: str = DEFAULT_BUNDLE_LABEL

    def has_override(self) -> bool:
        return bool(self.system_prompt or self.model_id)


# 모듈 레벨 TTL 캐시: (만료 시각, 값). 값이 None 이면 "오버라이드 없음"도 캐시한다
# (실패 시 재시도 폭주 방지).
_CACHE: tuple[float, BundleOverride | None] | None = None


def clear_cache() -> None:
    """TTL 캐시 초기화(테스트·강제 재조회용)."""
    global _CACHE
    _CACHE = None


def load_bundle_override(
    param_name: str,
    *,
    region: str = "us-west-2",
    ssm_client: Any | None = None,
    control_client: Any | None = None,
    now: float | None = None,
    use_cache: bool = True,
) -> BundleOverride | None:
    """활성 bundle 오버라이드를 조회한다. 실패·미설정 시 None.

    param_name 이 비어 있으면 기능 비활성으로 간주해 **즉시 None**(AWS 호출 없음).
    boto3 클라이언트는 주입 가능(단위 테스트용).
    """
    global _CACHE
    if not param_name or not param_name.strip():
        return None

    timestamp = time.monotonic() if now is None else now
    if use_cache and _CACHE is not None and _CACHE[0] > timestamp:
        return _CACHE[1]

    override = _fetch(param_name.strip(), region, ssm_client, control_client)
    if use_cache:
        _CACHE = (timestamp + CACHE_TTL_SECONDS, override)
    return override


def _fetch(
    param_name: str,
    region: str,
    ssm_client: Any | None,
    control_client: Any | None,
) -> BundleOverride | None:
    """실제 조회. 어떤 실패도 경고 로그 + None 으로 흡수한다."""
    pointer = _read_pointer(param_name, region, ssm_client)
    if pointer is None:
        return None
    bundle_id, version_id = pointer

    try:
        client = control_client if control_client is not None else _control_client(region)
        response = client.get_configuration_bundle_version(
            bundleId=bundle_id, versionId=version_id
        )
    except Exception as exc:  # noqa: BLE001 — 폴백 정책(AGENTREL04)
        logger.warning(
            "bundle 조회 실패(bundleId=%s versionId=%s) — 코드 기본값으로 진행: %s",
            bundle_id,
            version_id,
            exc,
        )
        return None

    configuration = _component_configuration(response)
    if configuration is None:
        logger.warning(
            "bundle components['%s'].configuration 을 찾을 수 없음 — 코드 기본값으로 진행",
            COMPONENT_KEY,
        )
        return None

    system_prompt = _clean_str(configuration.get("system_prompt"))
    model_id = _clean_str(configuration.get("model_id"))
    if not system_prompt and not model_id:
        logger.warning(
            "bundle 에 system_prompt/model_id 가 비어 있음 — 코드 기본값으로 진행 "
            "(bundleId=%s versionId=%s)",
            bundle_id,
            version_id,
        )
        return None

    override = BundleOverride(
        system_prompt=system_prompt,
        model_id=model_id,
        bundle_label=f"{bundle_id}@{version_id}",
    )
    logger.info(
        "bundle 오버라이드 적용: %s (system_prompt=%s, model_id=%s)",
        override.bundle_label,
        "yes" if system_prompt else "no",
        model_id or "-",
    )
    return override


def _read_pointer(
    param_name: str, region: str, ssm_client: Any | None
) -> tuple[str, str] | None:
    """SSM 파라미터에서 (bundleId, versionId) 를 읽는다. 실패 시 None."""
    try:
        client = ssm_client if ssm_client is not None else _ssm_client(region)
        response = client.get_parameter(Name=param_name)
        raw = (response.get("Parameter") or {}).get("Value")
        pointer = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:  # noqa: BLE001 — 폴백 정책(AGENTREL04)
        logger.warning(
            "활성 bundle 파라미터 조회 실패(%s) — 코드 기본값으로 진행: %s", param_name, exc
        )
        return None

    if not isinstance(pointer, dict):
        logger.warning("활성 bundle 파라미터 형식 오류(%s) — 코드 기본값으로 진행", param_name)
        return None
    bundle_id = _clean_str(pointer.get("bundleId") or pointer.get("bundle_id"))
    version_id = _clean_str(pointer.get("versionId") or pointer.get("version_id"))
    if not bundle_id or not version_id:
        logger.warning(
            "활성 bundle 파라미터에 bundleId/versionId 누락(%s) — 코드 기본값으로 진행",
            param_name,
        )
        return None
    return bundle_id, version_id


def _component_configuration(response: Any) -> dict[str, Any] | None:
    """응답에서 components[COMPONENT_KEY].configuration 을 방어적으로 추출."""
    if not isinstance(response, dict):
        return None
    components = response.get("components")
    if not isinstance(components, dict):
        return None
    component = components.get(COMPONENT_KEY)
    if not isinstance(component, dict):
        return None
    configuration = component.get("configuration")
    if isinstance(configuration, str):
        # document 타입이 문자열로 직렬화돼 오는 경우 방어.
        try:
            configuration = json.loads(configuration)
        except (json.JSONDecodeError, ValueError):
            return None
    return configuration if isinstance(configuration, dict) else None


def _ssm_client(region: str) -> Any:
    import boto3

    return boto3.client("ssm", region_name=region)


def _control_client(region: str) -> Any:
    import boto3

    return boto3.client("bedrock-agentcore-control", region_name=region)


def _clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
