{{/* Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms. */}}

{{/*
==============================================================================
LLM Gateway — Helm chart 공통 헬퍼
------------------------------------------------------------------------------
모든 서비스 템플릿에서 재사용하는 매크로를 정의합니다. DRY 원칙 준수.
==============================================================================
*/}}

{{/* ------------------------------------------------------------------------
  기본 이름 / Fullname
-------------------------------------------------------------------------- */}}
{{- define "llm-gateway.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "llm-gateway.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "llm-gateway.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* ------------------------------------------------------------------------
  공통 라벨 (metadata.labels 에 붙음)
-------------------------------------------------------------------------- */}}
{{- define "llm-gateway.labels" -}}
helm.sh/chart: {{ include "llm-gateway.chart" . }}
app.kubernetes.io/name: {{ include "llm-gateway.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: llm-gateway
{{- with .Values.global.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* ------------------------------------------------------------------------
  서비스별 컴포넌트 라벨 (기본 라벨 + component=<svc>)
  호출: {{ include "llm-gateway.componentLabels" (dict "root" . "component" "gateway-proxy") }}
-------------------------------------------------------------------------- */}}
{{- define "llm-gateway.componentLabels" -}}
{{ include "llm-gateway.labels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* ------------------------------------------------------------------------
  Selector 라벨 (Deployment selector.matchLabels / Service selector)
  immutable 해야 하므로 version 제외
-------------------------------------------------------------------------- */}}
{{- define "llm-gateway.selectorLabels" -}}
app.kubernetes.io/name: {{ include "llm-gateway.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* ------------------------------------------------------------------------
  이미지 레퍼런스 생성
  호출: {{ include "llm-gateway.image" (dict "root" . "image" .Values.gatewayProxy.image) }}
-------------------------------------------------------------------------- */}}
{{- define "llm-gateway.image" -}}
{{- $registry := .root.Values.global.imageRegistry -}}
{{- $repository := .image.repository -}}
{{- $tag := .image.tag | default .root.Chart.AppVersion -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry $repository $tag -}}
{{- else -}}
{{- printf "%s:%s" $repository $tag -}}
{{- end -}}
{{- end -}}

{{/* ------------------------------------------------------------------------
  ImagePullSecrets
-------------------------------------------------------------------------- */}}
{{- define "llm-gateway.imagePullSecrets" -}}
{{- with .Values.global.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/* ------------------------------------------------------------------------
  공통 어노테이션 (metadata.annotations)
-------------------------------------------------------------------------- */}}
{{- define "llm-gateway.commonAnnotations" -}}
{{- with .Values.global.commonAnnotations }}
{{- toYaml . }}
{{- end }}
{{- end -}}

{{/* ------------------------------------------------------------------------
  ServiceAccount 이름 (서비스별)
  호출: {{ include "llm-gateway.serviceAccountName" (dict "root" . "svc" .Values.gatewayProxy "default" "gateway-proxy") }}
-------------------------------------------------------------------------- */}}
{{- define "llm-gateway.serviceAccountName" -}}
{{- if .svc.serviceAccount.create -}}
{{- default (printf "%s-%s" (include "llm-gateway.fullname" .root) .default) .svc.serviceAccount.name -}}
{{- else -}}
{{- default "default" .svc.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* ------------------------------------------------------------------------
  DB URL 생성 (asyncpg URI 형식)
  admin-api는 "DATABASE_URL", 그 외는 "DB_URL" 사용 — 호출부에서 키 이름만 결정
-------------------------------------------------------------------------- */}}
{{- define "llm-gateway.dbUrl" -}}
{{- $db := .Values.database.external -}}
{{- printf "postgresql+asyncpg://%s:$(DB_PASSWORD)@%s:%d/%s?ssl=%s" $db.user $db.host (int $db.port) $db.name $db.sslMode -}}
{{- end -}}

{{- define "llm-gateway.dbUrlNotificationWorker" -}}
{{- $db := .Values.database.external -}}
{{- printf "postgresql+asyncpg://%s:$(DB_PASSWORD)@%s:%d/%s?ssl=%s" $db.notificationWorkerUser $db.host (int $db.port) $db.name $db.sslMode -}}
{{- end -}}

{{/* ------------------------------------------------------------------------
  DB_MASTER_URL — migration Job 이 init SQL 실행 + application user 생성/GRANT 용.
  libpq URL 형식 (psql 이 직접 읽으므로 asyncpg 드라이버 이름 불필요).
  password 는 컨테이너 env 의 $(DB_MASTER_PASSWORD) 로 치환됨.
-------------------------------------------------------------------------- */}}
{{- define "llm-gateway.dbMasterUrl" -}}
{{- $db := .Values.database.external -}}
{{- printf "postgresql://%s:$(DB_MASTER_PASSWORD)@%s:%d/%s?sslmode=%s" $db.masterUser $db.host (int $db.port) $db.name $db.sslMode -}}
{{- end -}}

{{/* ------------------------------------------------------------------------
  Redis URL 생성
-------------------------------------------------------------------------- */}}
{{- define "llm-gateway.redisUrl" -}}
{{- $r := .Values.redis.external -}}
{{- $scheme := "redis" -}}
{{- if $r.tls -}}
{{- $scheme = "rediss" -}}
{{- end -}}
{{- if $r.passwordSecretName -}}
{{- printf "%s://:$(REDIS_PASSWORD)@%s:%d/%d" $scheme $r.host (int $r.port) (int $r.db) -}}
{{- else -}}
{{- printf "%s://%s:%d/%d" $scheme $r.host (int $r.port) (int $r.db) -}}
{{- end -}}
{{- end -}}

{{/* ------------------------------------------------------------------------
  공통 env: DB_URL + DB_PASSWORD (Secret ref) + REDIS_URL + REDIS_PASSWORD
  호출:
    env:
      {{- include "llm-gateway.commonEnv" . | nindent 6 }}
-------------------------------------------------------------------------- */}}
{{- define "llm-gateway.commonEnv" -}}
- name: APP_ENV
  value: {{ .Values.global.environment | quote }}
- name: DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.external.passwordSecretName | quote }}
      key: {{ .Values.database.external.passwordSecretKey | quote }}
- name: DB_URL
  value: {{ include "llm-gateway.dbUrl" . | quote }}
- name: DATABASE_URL
  value: {{ include "llm-gateway.dbUrl" . | quote }}
{{- if .Values.redis.external.passwordSecretName }}
- name: REDIS_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.redis.external.passwordSecretName | quote }}
      key: {{ .Values.redis.external.passwordSecretKey | quote }}
{{- end }}
- name: REDIS_URL
  value: {{ include "llm-gateway.redisUrl" . | quote }}
- name: REDIS_CLUSTER_MODE
  value: {{ .Values.redis.external.clusterMode | default false | quote }}
- name: AWS_REGION
  value: {{ .Values.aws.region | quote }}
- name: AWS_DEFAULT_REGION
  value: {{ .Values.aws.region | quote }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ .Values.observability.otel.endpoint | quote }}
- name: OTEL_EXPORTER_OTLP_PROTOCOL
  value: {{ .Values.observability.otel.protocol | quote }}
{{- end -}}

{{/* ------------------------------------------------------------------------
  Admin-api/Gateway-proxy 전용 env: JWT / VK 암호화 키 / STS 등
-------------------------------------------------------------------------- */}}
{{- define "llm-gateway.authEnv" -}}
- name: VIRTUAL_KEY_ENCRYPTION_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.auth.virtualKey.encryptionKeySecretName | quote }}
      key: {{ .Values.auth.virtualKey.encryptionKeySecretKey | quote }}
- name: JWT_ALGORITHM
  value: {{ .Values.auth.jwt.algorithm | quote }}
- name: JWT_ISSUER
  value: {{ .Values.auth.jwt.issuer | quote }}
- name: JWT_AUDIENCE
  value: {{ .Values.auth.jwt.audience | quote }}
- name: JWT_JWKS_URI
  value: {{ .Values.auth.jwt.jwksUri | quote }}
- name: ALLOWED_STS_REGIONS
  value: {{ join "," .Values.aws.allowedStsRegions | quote }}
- name: ALLOWED_IAM_ROLES
  value: {{ join "," .Values.aws.allowedIamRoles | quote }}
{{- end -}}

{{/* ------------------------------------------------------------------------
  OIDC env (admin-api 전용) — Cognito / Keycloak / Okta / Azure AD 호환.
  adminApi.oidc.enabled=true 일 때만 주입.
-------------------------------------------------------------------------- */}}
{{- define "llm-gateway.oidcEnv" -}}
{{- if .Values.adminApi.oidc.enabled -}}
- name: OIDC_ISSUER_URL
  value: {{ .Values.adminApi.oidc.issuerUrl | quote }}
- name: OIDC_AUDIENCE
  value: {{ .Values.adminApi.oidc.audience | default "" | quote }}
- name: OIDC_PROVIDER_NAME
  value: {{ .Values.adminApi.oidc.providerName | quote }}
- name: OIDC_DISCOVERY_URL_OVERRIDE
  value: {{ .Values.adminApi.oidc.discoveryUrlOverride | default "" | quote }}
- name: OIDC_JWKS_CACHE_TTL_SECONDS
  value: {{ .Values.adminApi.oidc.jwksCacheTtlSeconds | quote }}
- name: OIDC_USER_ID_CLAIM
  value: {{ .Values.adminApi.oidc.userIdClaim | quote }}
- name: OIDC_EMAIL_CLAIM
  value: {{ .Values.adminApi.oidc.emailClaim | quote }}
- name: OIDC_NAME_CLAIM
  value: {{ .Values.adminApi.oidc.nameClaim | quote }}
- name: OIDC_GROUPS_CLAIM
  value: {{ .Values.adminApi.oidc.groupsClaim | quote }}
- name: OIDC_GROUP_PREFIX
  value: {{ .Values.adminApi.oidc.groupPrefix | default "Claude_" | quote }}
- name: OIDC_REJECT_UNMATCHED_GROUPS
  value: {{ .Values.adminApi.oidc.rejectUnmatchedGroups | quote }}
- name: OIDC_REQUIRED_GROUP
  value: {{ .Values.adminApi.oidc.requiredGroup | default "" | quote }}
- name: OIDC_VK_TTL_HOURS
  value: {{ .Values.adminApi.oidc.vkTtlHours | quote }}
- name: ADMIN_EMAILS
  value: {{ join "," .Values.adminApi.adminBootstrap.emails | quote }}
- name: ADMIN_GROUPS
  value: {{ join "," .Values.adminApi.adminBootstrap.groups | quote }}
- name: DEFAULT_TEAM_ID
  value: {{ .Values.adminApi.autoProvisioning.defaultTeamId | quote }}
- name: DEFAULT_DEPT_ID
  value: {{ .Values.adminApi.autoProvisioning.defaultDeptId | quote }}
- name: SYSTEM_USER_ID
  value: {{ .Values.adminApi.autoProvisioning.systemUserId | quote }}
{{- end -}}
{{- end -}}

{{/* ------------------------------------------------------------------------
  Downward API env (Pod 이름 → consumer name 등에 활용)
-------------------------------------------------------------------------- */}}
{{- define "llm-gateway.downwardApiEnv" -}}
- name: POD_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
- name: POD_NAMESPACE
  valueFrom:
    fieldRef:
      fieldPath: metadata.namespace
- name: POD_IP
  valueFrom:
    fieldRef:
      fieldPath: status.podIP
{{- end -}}

{{/* ------------------------------------------------------------------------
  서비스별 Service URL (클러스터 내부 DNS)
-------------------------------------------------------------------------- */}}
{{- define "llm-gateway.adminApiUrl" -}}
{{- printf "http://%s-admin-api.%s.svc.cluster.local:%d" (include "llm-gateway.fullname" .) .Release.Namespace (int .Values.adminApi.service.port) -}}
{{- end -}}

{{- define "llm-gateway.gatewayProxyUrl" -}}
{{- printf "http://%s-gateway-proxy.%s.svc.cluster.local:%d" (include "llm-gateway.fullname" .) .Release.Namespace (int .Values.gatewayProxy.service.port) -}}
{{- end -}}

{{/* ------------------------------------------------------------------------
  보안 컨텍스트 — 서비스별 override 병합
  호출: {{ include "llm-gateway.containerSecurityContext" (dict "root" . "override" .Values.adminUi.securityContextOverride) }}
-------------------------------------------------------------------------- */}}
{{- define "llm-gateway.containerSecurityContext" -}}
{{- $merged := deepCopy .root.Values.containerSecurityContext -}}
{{- if .override -}}
{{- $merged = merge .override $merged -}}
{{- end -}}
{{- toYaml $merged -}}
{{- end -}}
