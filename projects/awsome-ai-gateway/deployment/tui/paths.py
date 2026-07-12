# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""리포 내 배포 아티팩트 경로 상수. 하드코딩 경로를 한 곳에 모은다."""
from pathlib import Path

# paths.py = .../deployment/tui/paths.py → parents[2] = repo root
DEPLOY_DIR = Path(__file__).resolve().parents[1]          # .../deployment
REPO_ROOT = DEPLOY_DIR.parent                             # repo root
SCRIPTS_DIR = DEPLOY_DIR / "scripts"
TF_ENV_DIR = DEPLOY_DIR / "terraform" / "environments"
TOOL_TF_DIR = TF_ENV_DIR / "tool-gateway-dev"
BUILD_LAMBDAS_SH = REPO_ROOT / "admin-chat-agent" / "lambdas" / "build-lambdas.sh"


def llm_tf_dir(env: str) -> Path:
    """LLM Gateway terraform 환경 디렉토리 (env: dev|prod)."""
    return TF_ENV_DIR / f"llm-gateway-{env}"


def script(name: str) -> Path:
    """deployment/scripts/<name> 절대 경로."""
    return SCRIPTS_DIR / name
