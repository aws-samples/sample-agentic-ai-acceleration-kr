# EKS Fargate 배포 가이드

AWS EKS Fargate 환경에 LLM Gateway를 배포하는 전체 가이드입니다. **처음 배포하는 운영자가 이 문서만 보고 dev → prod 까지 완주 가능**하도록 작성되었습니다.

## 총 소요시간 (첫 배포)

| 환경 | 소요 |
|-----|-----|
| dev (2 AZ, Serverless Aurora) | **약 1~1.5시간** |
| prod (3 AZ, HA Aurora/ElastiCache) | **약 2~2.5시간** |

두 번째부터는 helm upgrade만 하면 되므로 **5~10분**.

## 문서 순서

반드시 **위에서 아래 순서**로 진행하세요. 각 단계는 앞 단계가 완료됐음을 전제로 합니다.

| # | 문서 | 내용 | 소요 |
|---|-----|------|-----|
| 01 | [prerequisites.md](./01-prerequisites.md) | AWS 계정/IAM/도구 설치 확인 | 15분 |
| 02 | [terraform-apply.md](./02-terraform-apply.md) | VPC → EKS → Aurora → ElastiCache 프로비저닝 | 45~60분 |
| 03 | [secrets.md](./03-secrets.md) | Secrets Manager에 비밀값 저장 | 15분 |
| 04 | [helm-install.md](./04-helm-install.md) | `install-eks.sh` 실행 | 15분 |
| 05 | [smoke-test.md](./05-smoke-test.md) | 배포 후 E2E 검증 | 10분 |
| 06 | [upgrade-rollback.md](./06-upgrade-rollback.md) | 버전 업데이트와 롤백 | 참고용 |
| 07 | [cognito-onboarding.md](./07-cognito-onboarding.md) | Cognito 첫 admin 등록 + OIDC end-to-end 검증 | 30~45분 |
| — | [troubleshooting.md](./troubleshooting.md) | 공통 이슈 (OIDC 섹션 포함) | 참고용 |

## 이 가이드의 규칙

### 명령어 실행 규칙

모든 명령어는 **리포 루트 디렉토리에서** 실행한다고 가정합니다:

```bash
cd /path/to/LLM-Gateway-Vanilla
pwd  # /Users/.../LLM-Gateway-Vanilla 여야 함
```

특정 하위 디렉토리에서 실행해야 하는 경우 문서에 명시됩니다.

### 환경 변수 규칙

**`dev` 환경** 기준으로 설명합니다. `prod` 는 문서 안에 `dev` 를 `prod` 로 치환하면 됩니다.

```bash
export ENV=dev          # 또는 prod
export AWS_REGION=ap-northeast-2
export AWS_PROFILE=claude-proxy-dev   # 본인 AWS profile
```

이 환경변수는 **매 터미널 세션마다** 다시 설정해야 합니다.

### 체크포인트 아이콘

- ✅ **확인**: 다음으로 넘어가기 전 반드시 이 출력이 나와야 함
- ⚠️ **주의**: 실수 잦은 지점
- 🔧 **선택사항**: 필요 시에만
- 🐛 **문제 발생 시**: [troubleshooting.md](./troubleshooting.md) 참조

## 시작하기 전에

- 이 가이드는 **AWS 계정의 관리자 권한**이 있다고 가정합니다
- **약 $200~400/월** dev 환경 비용 발생 (상세: [prerequisites.md](./01-prerequisites.md))
- 도중 실패해도 **terraform destroy** 로 전부 정리 가능 (무과금까지 돌릴 수 있음)

[👉 01-prerequisites.md 부터 시작](./01-prerequisites.md)
