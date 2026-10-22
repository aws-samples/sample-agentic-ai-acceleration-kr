# 09. EKS 1.31 → 1.36 업그레이드 (aws-load-balancer-controller v3 · External Secrets v1 포함)

> ⚠️ **기한 — EKS 1.31 의 extended support 는 2026-11-26 에 끝난다.** 그때까지 1.32 로 올리지 않으면
> AWS 가 컨트롤플레인을 강제로 업그레이드하고, 그 업그레이드는 롤백할 수 없다. 자동 업그레이드는
> 컨트롤플레인만 올리므로 애드온과 Fargate 파드의 kubelet 은 이전 버전으로 남는다. 또 extended support
> 구간의 클러스터(작성 시점 1.31 · 1.32 · 1.33)에는 클러스터당 시간당 **$0.50** 의 extended 프리미엄이 붙는다.
> 하드 기한은 첫 홉에만 있는 것이 아니다. 가장 가까운 것이 1.31 의 2026-11-26 이고, 1.32 · 1.33 도 각자의
> extended support 가 끝나는 날(작성 시점 1.32 는 2027-03-23, 1.33 은 2027-07-29) 같은 방식으로 강제
> 업그레이드된다. 그 날짜 전까지만 비용을 보며 일정을 조정할 수 있다. 날짜는 직접 확인한다:
> `aws eks describe-cluster-versions --cluster-versions 1.31 1.32 1.33 --query 'clusterVersions[].[clusterVersion,endOfStandardSupportDate,endOfExtendedSupportDate]' --output table`
>
> 이미 자동 업그레이드된 클러스터는 [아래 분기](#이미-aws-가-자동-업그레이드한-클러스터)를 먼저 본다.

이 레포의 기본값은 **EKS 1.36 / aws-load-balancer-controller chart 3.5.0 / external-secrets chart
2.11.0** 이다. 이 문서는 **옛 기본값(EKS 1.31, LBC chart 1.8.2, ESO chart 0.10.4)으로 만든 클러스터**를
따라 올리는 절차다. 신규 설치는 해당 없다 — [02](./02-terraform-apply.md) → [04](./04-helm-install.md)
를 그대로 따르면 처음부터 1.36 / LBC v3 / ESO v1 로 만들어진다.

> ⚠️ **옛 클러스터에 새 기본값을 그대로 apply 하지 말 것.** EKS 는 minor 를 한 칸씩만 올릴 수
> 있어 1.31 → 1.36 은 `InvalidParameterException` 이고, 그 apply 로 애드온·LBC·ESO 변경까지
> 한 plan 에 섞인다. 먼저 tfvars 로 **지금 라이브 값**을 고정한 뒤 아래 단계로 하나씩 올린다.

---

## 시작하기 전에

### 라이브 읽기

값을 정하기 전에 항상 먼저 읽는다.

```bash
aws eks describe-cluster --name <cluster> --query 'cluster.version' --output text
for a in coredns kube-proxy vpc-cni; do
  aws eks describe-addon --cluster-name <cluster> --addon-name "$a" \
    --query 'addon.[addonName,addonVersion,status]' --output text
done
helm --kube-context <ctx> list -A    # aws-load-balancer-controller / external-secrets 의 CHART 열
# ESO CRD 4종(externalsecrets · clusterexternalsecrets · secretstores · clustersecretstores)의 버전·저장버전
kubectl --context <ctx> get crd \
  -o jsonpath='{range .items[*]}{.metadata.name}{"  stored="}{.status.storedVersions}{"  "}{range .spec.versions[*]}{.name}(served={.served},storage={.storage}) {end}{"\n"}{end}' \
  | grep -E '^(externalsecrets|clusterexternalsecrets|secretstores|clustersecretstores)\.external-secrets\.io '
```

### 라이브 값 고정

새 main 을 apply 하기 전에, 환경 루트마다(`deployment/terraform/environments/llm-gateway-dev`,
`llm-gateway-prod`) `terraform.tfvars` 에 위에서 읽은 **라이브 값**을 그대로 적는다. 아래는 옛 기본값
그대로인 클러스터의 예다(`terraform.tfvars.example` 에 같은 블록이 주석으로 있다).

```hcl
# terraform.tfvars
eks_cluster_version = "1.31"
eks_addon_versions = {
  coredns    = "v1.11.3-eksbuild.1"
  kube_proxy = "v1.29.7-eksbuild.2"
  vpc_cni    = "v1.18.3-eksbuild.1"
}
alb_controller_chart_version   = "1.8.2"
external_secrets_chart_version = "0.10.4"
```

고정한 뒤의 `terraform plan` 은 **`No changes`** 여야 한다 — 이 값들로 인한 변경(EKS 버전 · 애드온 · 두
helm_release)은 물론 그 밖의 변경도 없어야 한다. 남는 변경은 어디에 남았는지로 나눠 다룬다.

- EKS 버전 · 애드온 · 두 helm_release 에 변경이 남으면 고정값이 라이브와 다르다는 뜻이다(값 오타, 빠진
  `eks_addon_versions` 블록 등). apply 하지 말고 라이브에서 읽은 값으로 고친다.
- 그 밖의 리소스에 남는 변경(새 main 이 함께 가져온 변경, 또는 라이브 드리프트)은 따로 검토해 **A 단계 전에
  별도 apply** 로 먼저 반영한다.

plan 이 `No changes` 가 된 뒤에 A 를 시작한다. 그래야 이후의 "의도한 델타만"(각 단계의 plan, E2E 게이트 7) ·
"정확히 1건"(B 의 2) 판정이 의미를 갖는다.
ESO 모듈은 webhook·cert-controller 비활성과 CRD 서빙 플래그를 chart major >= 1 일 때만 넣으므로,
0.10.x 로 고정한 동안에는 이전과 같은 values 로 렌더된다.

**고정을 지우는 기준.** 각 값은 라이브가 레포 기본값과 **정확히 같아졌을 때만** 지우고, 지운 뒤 plan 이 그
리소스에 대해 `No changes` 여야 한다. 애드온은 홉마다 재조회한 빌드를 쓰므로 레포 기본값과 다를 수 있다 —
그때는 `eks_addon_versions` 고정을 남겨 둔다(지우면 plan 이 애드온을 레포 기본값으로 되돌린다). 이 기준은
고정을 넣은 **모든 환경 루트**에 똑같이 적용한다.

### terraform CLI 로 진행한다 (deploy TUI 사용 금지)

`deployment/scripts/deploy-tui.sh` 는 이 업그레이드에 쓰지 않는다. TUI 는 `terraform.tfvars` 를 다시 쓰면서
최상위 단일 값(문자열 · bool · 정수)만 보존하고 `eks_addon_versions` 같은 블록 · 여러 줄 값 · 주석과 줄 끝에
주석이 붙은 값을 버린다(TUI 가 직접 채우는 `azs` · `eks_access_entries` · `tags` 만 예외). 이 재기록은 실행 확인
프롬프트보다 먼저 일어난다. 그리고 `terraform plan` 다음에 확인 없이
`terraform apply -auto-approve` 를 실행한다. 고정이 빠진 plan 이 그대로 적용될 수 있다. 각 단계는 환경
루트에서 `terraform plan` 을 읽고, 의도한 델타만 있을 때 `terraform apply` 한다.

### kubectl 클라이언트 버전

kubectl 은 클러스터 ±1 minor 안이어야 한다. 1.36 기준으로 설치한 kubectl(v1.35~v1.37,
[01 §2.2 / §2.3](./01-prerequisites.md))은 1.31~1.33 클러스터에서 지원 범위 밖이다. 홉마다
`kubectl version` 의 Client / Server 를 확인한다. 한 버전으로 덮이는 구간:

| 클러스터 | kubectl |
|---|---|
| 1.31 · 1.32 · 1.33 | v1.32.x |
| 1.34 · 1.35 · 1.36 | v1.35.x |

설치는 01 의 minor 고정 채널에서 버전만 바꿔 쓴다(예: `https://dl.k8s.io/release/stable-1.32.txt`).

### 이미 AWS 가 자동 업그레이드한 클러스터

`describe-cluster` 의 버전이 만들 때의 값보다 높으면(예: 1.31 로 만들었는데 라이브가 1.32) 자동 업그레이드가
지나간 것이다. 이 레포 모듈은 `upgradePolicy` 를 설정하지 않아 기본값(EXTENDED)이고, extended support 종료일에
자동 업그레이드된다. 따로 STANDARD 로 바꾼 클러스터는 표준지원 종료일(1.31 은 2025-11-26)에 이미 올라갔을 수 있다.

1. `eks_cluster_version` 을 **라이브 minor**(예: `"1.32"`)로 고정한다. 옛 값(1.31)이 남아 있으면 plan 이
   다운그레이드로, 새 기본값(1.36)이면 다중 점프로 `InvalidParameterException` 이다.
2. 애드온은 자동으로 올라가지 않는다. `eks_addon_versions` 를 지금 라이브 값으로 고정한 뒤 A 단계를
   **라이브 minor 의 기본값**으로 하고(아래 D 표의 그 minor 행이 참고값), D 의 ③(Fargate 파드 전량 재생성)을
   그 minor 에서 한 번 해서 kubelet 을 맞춘다.
3. B · C 는 그대로 하고, D 는 라이브 다음 minor 부터 시작한다(예: 라이브 1.32 → 1.33 홉부터).
4. 자동 업그레이드를 되돌리려 하지 말고 라이브 minor 를 출발점으로 삼는다(extended support 종료로 올라간
   경우 이전 버전은 이미 지원이 끝났다).

---

## 최종 상태 (이 레포 기본값)

| 항목 | 값 |
|---|---|
| 컨트롤플레인 | 1.36 |
| coredns | `v1.14.7-eksbuild.10` |
| kube-proxy | `v1.36.0-eksbuild.25` |
| vpc-cni | `v1.22.4-eksbuild.3` (1.31~1.36 전 구간 동일) |
| aws-load-balancer-controller | chart `3.5.0` / app `v3.5.0` |
| external-secrets | chart `2.11.0` / app `v2.11.0`, webhook·cert-controller 비활성 |
| ExternalSecret / ClusterSecretStore | `external-secrets.io/v1`, ESO CRD 4종 `storedVersions = ["v1"]` |

---

## 순서 (이 순서가 핵심이다)

| 단계 | 내용 | 왜 이 순서인가 |
|---|---|---|
| A | 지금 라이브 minor 에서 애드온을 그 minor 의 기본값으로 | 옛 kube-proxy(1.29)는 1.31 컨트롤플레인보다 2 마이너 뒤라 1.33 홉의 하드 블로커 |
| B | LBC chart 1.8.2 → 3.5.0 | v2.8.2 는 1.36 에서 검증된 적 없는 2024년 빌드. 컨트롤플레인 홉보다 먼저 |
| C | ESO 0.10.4 → 2.11.0 + apiVersion v1 | 2.x 는 v1beta1 을 serve 하지 않는다. 차트·스크립트·저장버전을 같이 옮겨야 한다 |
| D | 컨트롤플레인을 라이브 다음 minor 부터 1.36 까지 한 칸씩 | minor 는 한 칸씩. 홉마다 ①②③ |

prod 는 항상 dev 가 같은 단계를 통과한 뒤에 한다. 레포 기본값(LBC 3.5.0 / ESO 2.11.0)이 라이브보다 앞서
있으므로, [라이브 값 고정](#라이브-값-고정)에서 넣은 `alb_controller_chart_version` /
`external_secrets_chart_version` 을 B · C 단계에서 차례로 지운다 — 고정을 넣은 모든 환경 루트에서.

### A — 애드온을 지금 라이브 minor 의 기본값으로

`eks_cluster_version` 은 라이브 그대로 두고 `eks_addon_versions` 만 바꾼다. 라이브가 1.31 일 때의 예:

```hcl
# terraform.tfvars
eks_cluster_version = "1.31"
eks_addon_versions = {
  coredns    = "v1.11.4-eksbuild.60"
  kube_proxy = "v1.31.14-eksbuild.36"
  vpc_cni    = "v1.22.4-eksbuild.3"
}
```

라이브가 다른 minor 면(예: 자동 업그레이드된 1.32) 그 minor 의 기본값을 쓴다. 빌드 번호는 계속 올라가므로
적용 직전에 재조회한다:
`aws eks describe-addon-versions --kubernetes-version <라이브 minor> --addon-name coredns --query 'addons[0].addonVersions[?compatibilities[0].defaultVersion].addonVersion'`
(kube-proxy · vpc-cni 도 같은 방식). coredns 는 클러스터 DNS 라 버전 변경만으로 파드 롤이 생긴다.

### B — aws-load-balancer-controller 1.8.2 → 3.5.0

1. v3 CRD 를 **먼저 수동 apply** 한다. helm 은 chart 의 `crds/` 를 신규 설치 때만 만들고
   업그레이드하지 않는다. Gateway API 를 쓰지 않으므로 `crds/gateway-crds.yaml` 은 불필요하다.
   ```bash
   helm repo add eks https://aws.github.io/eks-charts && helm repo update eks
   helm pull eks/aws-load-balancer-controller --version 3.5.0 --untar --untardir /tmp/lbc-3.5.0
   kubectl --context <ctx> apply --server-side --force-conflicts \
     -f /tmp/lbc-3.5.0/aws-load-balancer-controller/crds/crds.yaml
   ```
   이 모듈이 쓰는 두 CRD 는 v2.8.2 와 버전 구조가 같다(TargetGroupBinding `v1alpha1`+`v1beta1`(storage),
   IngressClassParams `v1beta1`(storage), `storedVersions=["v1beta1"]`). 즉 신규 kind 2개
   (`ALBTargetControlConfig`, `GlobalAccelerator`) 가 추가될 뿐인 가산적 변경이고 저장버전 이전이 없다.
2. 고정을 넣은 **모든 환경 루트**에서(dev 먼저, prod 는 dev 가 E2E 게이트를 통과한 뒤) tfvars 의
   `alb_controller_chart_version` 줄을 지우고(레포 기본값 3.5.0) plan → **정확히 1건**(helm_release) 인지
   확인 → apply. 다른 버전으로 갈 때는 지우는 대신 그 값으로 바꾸고, 그 고정은 계속 남겨 둔다.
3. 확인: 이미지 `v3.5.0`, Ingress ADDRESS 가 이전과 **동일**(ALB 재생성 없음), TargetGroupBinding 의
   타깃 전부 healthy, LBC 로그 에러 0, `enableServiceMutatorWebhook: false` 유지.

참고: v3.0.0 부터 chart 버전 = app 버전이다(v2.x 는 chart 1.x). `enableCertManager=true` 사용자만 해당하는
v3.0.0 webhook 인증서 버그는 이 모듈과 무관하다(미설정).

### C — External Secrets 0.10.4 → 2.11.0, apiVersion v1

2.x 는 `v1` 이 served+storage 이고 `v1beta1` 은 chart 의 `crds.unsafeServeV1Beta1`(tfvars
`external_secrets_serve_v1beta1` → 모듈 `serve_v1beta1`, 기본 false) 일 때만 serve 된다. 이 플래그가 걸린 CRD 는
**4종**이다: `externalsecrets` · `clusterexternalsecrets` · `secretstores` · `clustersecretstores`
(`*.external-secrets.io`). ESO 는 CRD 가 chart 의 `templates/crds/` 에 있어 **helm 이 버전 업과 동시에 CRD 를
교체한다**(LBC 와 반대). 그래서 다리를 놓고 건넌다.

모듈은 webhook·cert-controller 비활성(`webhook.create=false`, `certController.create=false`,
`crds.conversion.enabled=false`)과 `crds.unsafeServeV1Beta1` 을 **chart major >= 1 일 때만** 넣는다. 0.10.4 로
고정해 둔 동안에는 아무것도 바뀌지 않고, 이 설정들은 2.11.0 으로 올리는 C2 의 apply 에 함께 들어간다.

- **C1 — 사전 확인 (0.10.4 그대로, 변경 없음).** [라이브 읽기](#라이브-읽기)의 CRD 출력에서 4종 모두
  `stored=` 에 `v1alpha1` 이 없어야 한다. 2.x CRD 는 `v1alpha1` 을 정의하지 않고 conversion 전략도 `None`
  이라, storedVersions 에 `v1alpha1` 이 남아 있으면 API 서버가 CRD 교체를 거부해 C2 의 helm upgrade 가
  실패한다. 남아 있으면 0.10.4 에서 그 kind 의 객체를 전부 재기록(C4 와 같은 `kubectl annotate`)한 뒤 그 CRD 의
  storedVersions 를 `["v1beta1"]` 로 맞추고 진행한다(0.10.x 는 webhook 이 살아 있으므로, 그 TLS 가 깨져 있으면
  annotate 가 admission 에서 막힌다 — [troubleshooting](./troubleshooting.md#증상-eso-webhook-tls-san-불일치--clustersecretstore-생성-실패)).
  객체 수도 세 둔다:
  ```bash
  kubectl --context <ctx> get externalsecrets,secretstores,clustersecretstores,clusterexternalsecrets,pushsecrets -A
  ```
- **C2 — 2.11.0 으로, 다리 ON.** 고정을 넣은 모든 환경 루트(dev 먼저)의 tfvars 에서
  `external_secrets_chart_version` 줄을 지우고(레포 기본값 2.11.0) `external_secrets_serve_v1beta1 = true` 를
  넣어 apply 한다. 이 apply 하나에 chart 업그레이드, CRD 교체, webhook·cert-controller Deployment 와 VWC
  제거가 함께 들어간다. 결과: `v2.11.0` 가동, CRD 4종이 `v1(served,storage)` + `v1beta1(served)` 동시 serve,
  `storedVersions=["v1beta1","v1"]`, 기존 객체 `SecretSynced` 유지, `external-secrets-webhook` ·
  `external-secrets-cert-controller` 없음.
  > ⚠️ **C2 는 되돌릴 수 없다(0.10.x 로 복귀 불가).** 2.11.0 CRD 가 적용되면 API 서버가 4종의 `status.storedVersions`
  > 에 `v1` 을 기록한다. 0.10.4 CRD 는 `v1alpha1` · `v1beta1` 만 정의하는데, API 서버는 storedVersions 에 있는
  > 버전이 `spec.versions` 에서 빠지는 CRD 업데이트를 거부한다. 그래서 실패한 C2 의 자동 롤백(모듈 helm_release 의
  > `atomic = true`)도, 나중에 0.10.x 로 다시 고정하는 것도 CRD 를 되돌리지 못한다. apply 가 실패하면
  > `helm rollback` 도 0.10.x 재고정도 하지 말고, 원인을 고친 뒤 **같은 terraform apply 를 다시 실행**한다.
  > `external_secrets_serve_v1beta1 = true` 는 C4 까지 유지한다 — 롤백이 일부만 적용돼 컨트롤러가 이전 버전으로
  > 남더라도 v1beta1 로 계속 동기화할 수 있게 하기 위해서다.
  >
  > 0.10.x chart 기본값(webhook · cert-controller 활성, CRD conversion `Webhook`) 그대로인 클러스터에서는 chart major
  > 변경 · webhook · cert-controller 제거 · conversion `None` 전환이 이 한 apply 에 같이 들어가는 것이 정상 경로다.
  > 그래서 prod 의 C2 는 dev 가 C2 뒤 [E2E 게이트](#e2e-게이트-모든-단계-공통-하나라도-실패하면-다음-단계-금지)를
  > 통과한 다음에만 한다.
- **C3 — llm-gateway 를 v1 로.** 현재 차트는 클러스터가 `external-secrets.io/v1` 을 serve 하면 v1 로
  렌더한다(`llm-gateway.externalSecretsApiVersion`). `helm upgrade` 한 번이면 ExternalSecret 3개가
  v1 로 바뀐다. ⚠️ 이 전환은 `checksum/secret` 을 바꿔 **6개 Deployment 전부가 롤링 재시작**된다
  (scheduler 는 `Recreate` 라 그동안 cron 싱글턴이 0개) — 유지보수 창에서 할 것.
- **C4 — 저장버전 수렴, 다리 OFF.** storedVersions 에 `v1beta1` 이 남은 `*.external-secrets.io` CRD 를 전부
  찾아(4종 — 객체가 0개인 kind 도 storedVersions 에는 `v1beta1` 이 남는다) 그 kind 의 객체를 재기록해 v1 로
  저장시키고 storedVersions 를 `["v1"]` 로 맞춘다. 조회나 재기록이 실패하면 그 CRD 는 패치하지 않고 멈춘다 —
  재기록되지 않은 객체가 남은 채로 storedVersions 에서 `v1beta1` 을 빼면, 상류가 v1beta1 정의를 지우는 날 그
  객체를 읽을 수 없게 된다.
  ```bash
  CTX=<ctx>
  for crd in $(kubectl --context "$CTX" get crd \
      -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.storedVersions}{"\n"}{end}' \
      | awk '$1 ~ /\.external-secrets\.io$/ && $2 ~ /"v1beta1"/ {print $1}'); do
    echo "== $crd"
    objs=$(kubectl --context "$CTX" get "$crd" -A -o name) || { echo "조회 실패: $crd — 중단"; break; }
    if [ -n "$objs" ]; then
      kubectl --context "$CTX" annotate "$crd" --all -A eso-v1-rewrite="$(date +%s)" --overwrite \
        || { echo "재기록 실패: $crd — 중단"; break; }
    fi
    kubectl --context "$CTX" patch crd "$crd" --subresource=status --type=merge \
      -p '{"status":{"storedVersions":["v1"]}}'
  done
  # 확인: 아무것도 출력되지 않아야 한다
  kubectl --context "$CTX" get crd \
    -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.storedVersions}{"\n"}{end}' \
    | awk '$1 ~ /\.external-secrets\.io$/ && $2 ~ /"v1beta1"/'
  ```
  그다음 고정을 넣은 모든 환경 루트의 tfvars 에서 `external_secrets_serve_v1beta1` 줄을 지우고(기본 false)
  apply 한다. `served: false` 는 `spec.versions` 에서 항목을 지우는 것이 아니라서 수렴 전에도 플래그 OFF
  자체는 허용된다. 그래도 수렴시키는 이유는, chart 주석이 예고한 대로 상류가 v1beta1 항목을 완전히 지우는
  날 CRD 업데이트가 거부되지 않게 하기 위해서다.

결과: CRD 4종 `v1beta1(served=false)` · `storedVersions=["v1"]`, ExternalSecret 3개 + ClusterSecretStore 1개가
`external-secrets.io/v1` 이고 `Ready=True`.

### D — 컨트롤플레인 한 칸씩 (라이브 다음 minor → 1.36)

각 홉은 세 번의 별도 단계다. ①과 ②를 같은 apply 에 넣지 않는다. 홉 전에 kubectl 클라이언트가 새 minor 의
±1 안인지 본다([kubectl 클라이언트 버전](#kubectl-클라이언트-버전)).

1. **① 컨트롤플레인** — tfvars `eks_cluster_version` 을 한 칸 올려 apply.
   ①이 끝나도 애드온과 노드 kubelet 은 이전 버전이다(컨트롤플레인만 새 버전인 구간이 실재한다).
2. **② 애드온** — 아래 표의 그 버전 값(적용 직전 재조회)으로 `eks_addon_versions` 를 바꿔 apply.
   ②를 ①보다 먼저 하면 kube-proxy 가 apiserver 보다 새로워져 skew 위반이다.
3. **③ Fargate 파드 전량 재생성** — kubelet 은 파드를 다시 만들 때만 바뀐다. ① 직후 몇 분은
   Fargate 가 아직 이전 kubelet 노드에 파드를 배정할 수 있으므로 **① 후 3분 이상 기다린 뒤** 시작한다.
   인프라 티어(coredns → aws-load-balancer-controller → external-secrets)는 순차로, 나머지는 병렬로
   `kubectl rollout restart` 하고 한 번에 수렴을 기다린다. 끝나면 이전 버전 kubelet 노드가 남았는지
   보고, 남았으면 그 노드의 파드 소유 워크로드만 다시 롤한다.
   ```bash
   kubectl --context <ctx> get nodes -o custom-columns='NAME:.metadata.name,KUBELET:.status.nodeInfo.kubeletVersion'
   ```

참고값 (ap-northeast-2, 작성 시점 `describe-addon-versions` 의 defaultVersion — 적용 직전에 재조회할 것):

| 홉 | coredns | kube-proxy | vpc-cni |
|---|---|---|---|
| 1.32 | `v1.11.4-eksbuild.79` | `v1.32.13-eksbuild.32` | `v1.22.4-eksbuild.3` |
| 1.33 | `v1.12.4-eksbuild.57` (마이너 ⬆) | `v1.33.10-eksbuild.29` | 동일 |
| 1.34 | 동일 | `v1.34.6-eksbuild.29` | 동일 |
| 1.35 | `v1.13.2-eksbuild.50` (마이너 ⬆) | `v1.35.3-eksbuild.29` | 동일 |
| 1.36 | `v1.14.7-eksbuild.10` (마이너 ⬆) | `v1.36.0-eksbuild.25` | 동일 |

1.34 홉만 coredns·vpc-cni 가 직전과 같아 ②에서 DNS 롤이 없다. 그 홉에서는 coredns 가 ③의 첫 순간에만
재생성되므로 이전 kubelet 노드에 배정되기 쉽다 — 위 "3분 대기 + 잔존 노드 재롤" 이 그래서 필요하다.

**모든 홉이 끝난 뒤 고정 정리.** tfvars 의 `eks_cluster_version` 은 라이브가 `1.36` 이면 지운다(레포 기본값과
같다). `eks_addon_versions` 는 라이브 애드온 버전이 레포 기본값(위 [최종 상태](#최종-상태-이-레포-기본값) 표)과
**정확히 같을 때만** 지운다 — ②에서 재조회한 빌드가 그보다 새 빌드라면 그대로 남겨 둔다(지우면 plan 이 애드온을
레포 기본값으로 되돌린다). 어느 쪽이든 정리 뒤 plan 이 `No changes` 여야 한다. 고정을 넣은 모든 환경 루트에서
같은 기준으로 한다.

---

## E2E 게이트 (모든 단계 공통, 하나라도 실패하면 다음 단계 금지)

1. `deployment/scripts/smoke-test.sh --env <dev|prod> --context <ctx> --with-bedrock` — 롤아웃 직후의 종료 중
   파드가 total 에 잡힐 수 있으므로 Deployment `ready == desired == available` 을 먼저 기다리고 최대 3회 재시도
2. HPA 메트릭 — [05 §1.5](./05-smoke-test.md). ③ 직후 1분가량 HPA `CURRENT` 가 `<none>` 일 수 있다(재시도로 판정)
3. VK 발급 → `/v1/models` → Bedrock 실호출 → 비용 기록 — [05 §2.1 / §2.2 / §2.4 / §2.5](./05-smoke-test.md)
4. ExternalSecret 전 객체 `SecretSynced`, 대상 Secret 존재
5. Ingress ADDRESS 유지 + 그 주소로 200, LBC 로그 reconcile 에러 0
6. `aws eks list-insights` — **`UPGRADE_READINESS` 의 ERROR 0** 으로 판정한다(아래 함정 참고)
7. `terraform plan` 이 의도한 델타 외 0

---

## 함정

- **`ROLLBACK_READINESS` ERROR 는 장애가 아니다.** ③을 끝내면 "Kubelet / kube-proxy / add-on version
  rollback compatibility" 가 ERROR 로 뜬다. 노드 kubelet 이 컨트롤플레인과 같아졌다는 뜻 = ③을 제대로
  끝낸 증거이고, 동시에 그 버전으로의 롤백 창이 닫혔다는 뜻이다.
- **insight 는 이벤트가 아니라 주기 갱신이다.** 애드온을 올린 뒤에도 `kube-proxy version skew` WARNING 이
  `lastRefreshTime` 이 갱신될 때까지 남는다(10시간 이상 걸릴 수 있다). skew 는 ground truth 로 판정한다:
  `describe-cluster.version` vs kube-proxy DaemonSet 이미지 태그의 마이너 vs 노드 `kubeletVersion`.
- **`kubectl run` 으로 임시 파드를 띄우는 검증은 Fargate 에서 못 쓴다.** `default` 네임스페이스는 Fargate
  프로파일에 없어 파드가 영구 Pending 이고 `timed out waiting for the condition` 으로 끝난다(DNS 장애로
  오독하기 쉽다). DNS 는 기존 앱 파드에 `kubectl exec` 해서 클러스터 서비스명 · 다른 네임스페이스 ·
  `bedrock-runtime.<region>.amazonaws.com`(사설 IP) 세 가지를 해석시키고 coredns 로그 에러 0 을 본다.
- **ESO webhook 은 Ready 여도 깨져 있을 수 있다.** 서빙 인증서 SAN 이 노드 호스트명이면 모든 admission 이
  TLS 실패하고(`failurePolicy=Fail`), 그 상태로는 차트의 ExternalSecret `pre-upgrade` 훅이 막혀
  `helm upgrade` 가 불가능하다. 파드 상태가 아니라 실제 admission 요청(예: `kubectl annotate`)으로 판정한다.
  이 레포 모듈이 chart 1.x 이상에서 webhook·cert-controller 를 끄는 이유다.
- **홉 plan 의 유령 변경 두 가지.** (1) `aws_iam_openid_connect_provider` 의 `thumbprint_list` 가
  `(known after apply)` 로 잡힌다(`data.tls_certificate` 재조회). 루트 CA thumbprint 를 독립 계산해 라이브
  등록값과 같은지 보고, 기능 증명은 Bedrock E2E(IRSA → STS → InvokeModel)로 한다. `time_sleep` 교체는
  `cluster_version` 트리거라 무해하다. (2) `module.eks` 에 pending 변경이 있으면 ALB Controller IRSA 의
  policy/role 이 "update" 로 보인다. 적용 전후 라이브 정책 문서를 정렬 비교해 판정한다.
- **`upgradePolicy.supportType` 은 정책 설정이지 현재 상태가 아니다.** `EXTENDED` 는 "표준지원이 끝나면
  extended 로 넘어간다" 는 뜻이다. 과금 여부는 현재 버전이 표준지원 종료일을 지났는지로 판단한다
  (1.36 의 표준지원은 2027-08-02 까지).
- **롤백.** in-place 업그레이드 7일 이내, 직전 minor 한 칸, 클러스터 ACTIVE + ERROR insight 0 일 때만
  가능하다. Fargate 는 ③ 뒤에는 새 파드를 지워야 하므로 사실상 **계획된 다운타임**이다.
- **zsh.** `K="kubectl --context x"; $K get pods` 는 단어 분할이 안 돼 command not found 다(`${=K}` 를 쓰거나
  직접 호출). 한글을 변수 바로 뒤에 붙이면(`$V노드`) 변수명으로 파싱된다(`${V}노드`). 에러 출력을 버리지
  말고, 스크립트는 `zsh -n` 으로 먼저 검사한다.
