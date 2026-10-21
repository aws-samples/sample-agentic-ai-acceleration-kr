# 09. EKS 1.31 → 1.36 업그레이드 (aws-load-balancer-controller v3 · External Secrets v1 포함)

이 레포의 기본값은 **EKS 1.36 / aws-load-balancer-controller chart 3.5.0 / external-secrets chart
2.11.0** 이다. 레퍼런스 dev·prod 는 이 문서의 순서대로 1.31 → 1.36 을 한 칸씩 완주했다
(2026-10-07 ~ 2026-10-12, 홉마다 전체 E2E 게이트 통과).

이 문서는 **옛 기본값(EKS 1.31, LBC chart 1.8.2, ESO chart 0.10.4)으로 만든 클러스터**를 따라
올리는 절차다. 신규 설치는 해당 없다 — [02](./02-terraform-apply.md) → [04](./04-helm-install.md)
를 그대로 따르면 처음부터 1.36 / LBC v3 / ESO v1 로 만들어진다.

> ⚠️ **옛 클러스터에 새 기본값을 그대로 apply 하지 말 것.** EKS 는 minor 를 한 칸씩만 올릴 수
> 있어 1.31 → 1.36 은 `InvalidParameterException` 이고, 그 apply 로 애드온·LBC·ESO 변경까지
> 한 plan 에 섞인다. 먼저 tfvars 로 **지금 라이브 값**을 고정한 뒤 아래 단계로 하나씩 올린다.

---

## 최종 상태 (레퍼런스 dev·prod)

컨트롤플레인·애드온은 dev·prod 2026-10-22 실측, LBC·ESO 는 dev 2026-10-22 실측(prod 는 2026-10-12 적용)이다.

| 항목 | 값 |
|---|---|
| 컨트롤플레인 | 1.36 |
| coredns | `v1.14.7-eksbuild.10` |
| kube-proxy | `v1.36.0-eksbuild.25` |
| vpc-cni | `v1.22.4-eksbuild.3` (1.31~1.36 전 구간 동일) |
| aws-load-balancer-controller | chart `3.5.0` / app `v3.5.0` |
| external-secrets | chart `2.11.0` / app `v2.11.0`, webhook·cert-controller 비활성 |
| ExternalSecret / ClusterSecretStore | `external-secrets.io/v1`, CRD `storedVersions = ["v1"]` |

라이브 읽기 (값을 정하기 전에 항상 먼저):

```bash
aws eks describe-cluster --name <cluster> --query 'cluster.version' --output text
for a in coredns kube-proxy vpc-cni; do
  aws eks describe-addon --cluster-name <cluster> --addon-name "$a" \
    --query 'addon.[addonName,addonVersion,status]' --output text
done
helm --kube-context <ctx> list -A
kubectl --context <ctx> get crd externalsecrets.external-secrets.io \
  -o jsonpath='{range .spec.versions[*]}{.name}(served={.served},storage={.storage}) {end}{.status.storedVersions}{"\n"}'
```

---

## 순서 (이 순서가 핵심이다)

| 단계 | 내용 | 왜 이 순서인가 |
|---|---|---|
| A | 1.31 에서 애드온을 1.31 기본값으로 | 옛 kube-proxy(1.29)는 컨트롤플레인보다 2 마이너 뒤라 1.33 홉의 하드 블로커 |
| B | LBC chart 1.8.2 → 3.5.0 | v2.8.2 는 1.36 에서 검증된 적 없는 2024년 빌드. 컨트롤플레인 홉보다 먼저 |
| C | ESO 0.10.4 → 2.11.0 + apiVersion v1 | 2.x 는 v1beta1 을 serve 하지 않는다. 차트·스크립트·저장버전을 같이 옮겨야 한다 |
| D | 컨트롤플레인 1.32 → 1.33 → 1.34 → 1.35 → 1.36 | minor 는 한 칸씩. 홉마다 ①②③ |

prod 는 항상 dev 가 같은 단계를 통과한 뒤에 한다. 모듈 기본값(LBC 3.5.0 / ESO 2.11.0)이 이미 올라가
있으므로, 단계별로 나누려면(권장) 각 환경 루트의 `module "alb_controller"` / `module "external_secrets"` 에
`chart_version = "1.8.2"` / `"0.10.4"` 를 잠시 넣고 B·C 단계에서 지운다.

### A — 애드온을 1.31 기본값으로

```hcl
# terraform.tfvars
eks_cluster_version = "1.31"
eks_addon_versions = {
  coredns    = "v1.11.4-eksbuild.60"
  kube_proxy = "v1.31.14-eksbuild.36"
  vpc_cni    = "v1.22.4-eksbuild.3"
}
```

빌드 번호는 계속 올라가므로 적용 직전에 재조회한다:
`aws eks describe-addon-versions --kubernetes-version 1.31 --addon-name coredns --query 'addons[0].addonVersions[?compatibilities[0].defaultVersion].addonVersion'`
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
   우리가 쓰는 두 CRD 는 v2.8.2 와 버전 구조가 같다(TargetGroupBinding `v1alpha1`+`v1beta1`(storage),
   IngressClassParams `v1beta1`(storage), `storedVersions=["v1beta1"]`). 즉 신규 kind 2개
   (`ALBTargetControlConfig`, `GlobalAccelerator`) 가 추가될 뿐인 가산적 변경이고 저장버전 이전이 없다.
2. (prod) `chart_version` 고정을 지우고 plan → **정확히 1건**(helm_release) 인지 확인 → apply.
3. 확인: 이미지 `v3.5.0`, Ingress ADDRESS 가 이전과 **동일**(ALB 재생성 없음), TargetGroupBinding 의
   타깃 전부 healthy, LBC 로그 에러 0, `enableServiceMutatorWebhook: false` 유지.

참고: v3.0.0 부터 chart 버전 = app 버전이다(v2.x 는 chart 1.x). `enableCertManager=true` 사용자만 해당하는
v3.0.0 webhook 인증서 버그는 이 모듈과 무관하다(미설정).

### C — External Secrets 0.10.4 → 2.11.0, apiVersion v1

2.x 는 `v1` 이 served+storage 이고 `v1beta1` 은 `crds.unsafeServeV1Beta1`(= 모듈 변수
`serve_v1beta1`, 기본 false) 일 때만 serve 된다. ESO 는 CRD 가 chart 의 `templates/crds/` 에 있어
**helm 이 버전 업과 동시에 CRD 를 교체한다**(LBC 와 반대). 그래서 다리를 놓고 건넌다.

- **C1 — webhook·cert-controller 끄기 (chart 는 0.10.4 그대로).** 모듈은 이제 `webhook.create=false`,
  `certController.create=false`, `crds.conversion.enabled=false` 를 넣는다. 먼저 이것만 적용해서
  ESO 업그레이드와 분리한다. 적용 전에 `storedVersions` 가 `["v1beta1"]` 인지(v1alpha1 저장 객체가
  없는지) 확인한다 — conversion 전략이 `None` 이 되기 때문이다.
- **C2 — 2.11.0 으로, 다리 ON.** `module "external_secrets"` 에 `serve_v1beta1 = true` 를 넣고
  (prod 는 `chart_version` 고정도 지우고) apply. 결과: `v2.11.0` 가동, CRD 3종이
  `v1(served,storage)` + `v1beta1(served)` 동시 serve, `storedVersions=["v1beta1","v1"]`,
  기존 객체 `SecretSynced` 유지.
- **C3 — llm-gateway 를 v1 로.** 현재 차트는 클러스터가 `external-secrets.io/v1` 을 serve 하면 v1 로
  렌더한다(`llm-gateway.externalSecretsApiVersion`). `helm upgrade` 한 번이면 ExternalSecret 3개가
  v1 로 바뀐다. ⚠️ 이 전환은 `checksum/secret` 을 바꿔 **6개 Deployment 전부가 롤링 재시작**된다
  (scheduler 는 `Recreate` 라 그동안 cron 싱글턴이 0개) — 유지보수 창에서 할 것.
- **C4 — 저장버전 수렴, 다리 OFF.** ESO 객체를 전부 재기록해 v1 로 저장시킨 뒤 CRD 3종의
  `storedVersions` 를 `["v1"]` 로 맞추고, `serve_v1beta1` 줄을 지워 apply 한다.
  ```bash
  # 객체 수부터 센다 (SecretStore/ClusterExternalSecret/PushSecret 등이 있으면 같이 재기록)
  kubectl --context <ctx> get externalsecrets,secretstores,clustersecretstores,clusterexternalsecrets,pushsecrets -A
  kubectl --context <ctx> annotate externalsecrets.external-secrets.io --all -A eso-v1-rewrite="$(date +%s)" --overwrite
  kubectl --context <ctx> annotate clustersecretstores.external-secrets.io --all eso-v1-rewrite="$(date +%s)" --overwrite
  for c in externalsecrets clustersecretstores secretstores; do
    kubectl --context <ctx> patch crd "$c.external-secrets.io" --subresource=status --type=merge \
      -p '{"status":{"storedVersions":["v1"]}}'
  done
  ```
  `served: false` 는 `spec.versions` 에서 항목을 지우는 것이 아니라서 수렴 전에도 플래그 OFF 자체는
  허용된다. 그래도 수렴시키는 이유는, chart 주석이 예고한 대로 상류가 v1beta1 항목을 완전히 지우는
  날 CRD 업데이트가 거부되지 않게 하기 위해서다.

결과: CRD 3종 `v1beta1(served=false)`, ExternalSecret 3개 + ClusterSecretStore 1개가
`external-secrets.io/v1` 이고 `Ready=True`.

### D — 컨트롤플레인 한 칸씩 (1.32 → 1.36)

각 홉은 세 번의 별도 단계다. ①과 ②를 같은 apply 에 넣지 않는다.

1. **① 컨트롤플레인** — tfvars `eks_cluster_version` 을 한 칸 올려 apply.
   ①이 끝나도 애드온과 노드 kubelet 은 이전 버전이다(컨트롤플레인만 새 버전인 구간이 실재한다).
2. **② 애드온** — 아래 표의 그 버전 값으로 `eks_addon_versions` 를 바꿔 apply.
   ②를 ①보다 먼저 하면 kube-proxy 가 apiserver 보다 새로워져 skew 위반이다.
3. **③ Fargate 파드 전량 재생성** — kubelet 은 파드를 다시 만들 때만 바뀐다. ① 직후 몇 분은
   Fargate 가 아직 이전 kubelet 노드에 파드를 배정할 수 있으므로 **① 후 3분 이상 기다린 뒤** 시작한다.
   인프라 티어(coredns → aws-load-balancer-controller → external-secrets)는 순차로, 나머지는 병렬로
   `kubectl rollout restart` 하고 한 번에 수렴을 기다린다. 끝나면 이전 버전 kubelet 노드가 남았는지
   보고, 남았으면 그 노드의 파드 소유 워크로드만 다시 롤한다.
   ```bash
   kubectl --context <ctx> get nodes -o custom-columns='NAME:.metadata.name,KUBELET:.status.nodeInfo.kubeletVersion'
   ```

실제로 적용한 값 (ap-northeast-2, 홉 시점 `describe-addon-versions` 의 defaultVersion — 적용 직전에 재조회할 것):

| 홉 | coredns | kube-proxy | vpc-cni |
|---|---|---|---|
| 1.32 | `v1.11.4-eksbuild.79` | `v1.32.13-eksbuild.32` | `v1.22.4-eksbuild.3` |
| 1.33 | `v1.12.4-eksbuild.57` (마이너 ⬆) | `v1.33.10-eksbuild.29` | 동일 |
| 1.34 | 동일 | `v1.34.6-eksbuild.29` | 동일 |
| 1.35 | `v1.13.2-eksbuild.50` (마이너 ⬆) | `v1.35.3-eksbuild.29` | 동일 |
| 1.36 | `v1.14.7-eksbuild.10` (마이너 ⬆) | `v1.36.0-eksbuild.25` | 동일 |

1.34 홉만 coredns·vpc-cni 가 직전과 같아 ②에서 DNS 롤이 없다. 그 홉에서는 coredns 가 ③의 첫 순간에만
재생성되므로 이전 kubelet 노드에 배정되기 쉽다 — 위 "3분 대기 + 잔존 노드 재롤" 이 그래서 필요하다.

모든 홉이 끝나면 tfvars 의 `eks_cluster_version` · `eks_addon_versions` 를 지워 레포 기본값과 같게 둔다
(plan `No changes` 여야 한다).

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

## 함정 (실측으로 확인된 것)

- **`ROLLBACK_READINESS` ERROR 는 장애가 아니다.** ③을 끝내면 "Kubelet / kube-proxy / add-on version
  rollback compatibility" 가 ERROR 로 뜬다. 노드 kubelet 이 컨트롤플레인과 같아졌다는 뜻 = ③을 제대로
  끝낸 증거이고, 동시에 그 버전으로의 롤백 창이 닫혔다는 뜻이다.
- **insight 는 이벤트가 아니라 주기 갱신이다.** 애드온을 올린 뒤에도 `kube-proxy version skew` WARNING 이
  `lastRefreshTime` 이 갱신될 때까지(10시간 이상) 남아 있었다. skew 는 ground truth 로 판정한다:
  `describe-cluster.version` vs kube-proxy DaemonSet 이미지 태그의 마이너 vs 노드 `kubeletVersion`.
- **`kubectl run` 으로 임시 파드를 띄우는 검증은 Fargate 에서 못 쓴다.** `default` 네임스페이스는 Fargate
  프로파일에 없어 파드가 영구 Pending 이고 `timed out waiting for the condition` 으로 끝난다(DNS 장애로
  오독하기 쉽다). DNS 는 기존 앱 파드에 `kubectl exec` 해서 클러스터 서비스명 · 다른 네임스페이스 ·
  `bedrock-runtime.<region>.amazonaws.com`(사설 IP) 세 가지를 해석시키고 coredns 로그 에러 0 을 본다.
- **ESO webhook 은 Ready 여도 깨져 있을 수 있다.** 서빙 인증서 SAN 이 노드 호스트명이면 모든 admission 이
  TLS 실패하고(`failurePolicy=Fail`), 그 상태로는 차트의 ExternalSecret `pre-upgrade` 훅이 막혀
  `helm upgrade` 가 불가능하다. 파드 상태가 아니라 실제 admission 요청(예: `kubectl annotate`)으로 판정한다.
  이 레포 모듈이 webhook·cert-controller 를 끄는 이유다.
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
