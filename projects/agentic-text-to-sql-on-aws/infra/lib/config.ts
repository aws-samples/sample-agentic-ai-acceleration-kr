import { Construct } from 'constructs';

/**
 * 스택 전반에서 공유하는 설정 값. cdk.json 의 context 에서 읽어오며,
 * 다른 에이전트들과 합의된 "공통 계약"(리소스 접두어, ECR 리포명, DB/인덱스명 등)의
 * 단일 진실 원천이다. 시크릿·계정 ID 는 여기에 하드코딩하지 않는다.
 */
export interface EcrRepoNames {
  readonly orchestrator: string;
  readonly sqlExecutionMcp: string;
  readonly semanticRetrievalMcp: string;
  readonly ui: string;
  // ── M4: admin panel ──
  /** datasource-admin-mcp 이미지 리포 (agentic-t2sql/datasource-admin-mcp) */
  readonly datasourceAdminMcp: string;
  /** admin web(Next.js) 이미지 리포 (agentic-t2sql/admin-web) */
  readonly adminWeb: string;
}

export interface AppConfig {
  /** 리소스 접두어 (agentic-t2sql) */
  readonly appPrefix: string;
  /** 배포 리전 (us-west-2 고정) */
  readonly region: string;
  /** Aurora 데이터베이스명 (ecommerce) */
  readonly dbName: string;
  /** read-only DB 사용자명 (agent_ro) */
  readonly readOnlyDbUser: string;
  /** OpenSearch 인덱스명 (t2sql-schema-docs) */
  readonly openSearchIndex: string;
  /** 임베딩 모델 ID (Titan embed v2) */
  readonly embeddingModelId: string;
  /** 오케스트레이터 LLM 모델 ID */
  readonly modelId: string;
  /** ECR 리포 이름 4종 */
  readonly ecrRepos: EcrRepoNames;

  // ───────────── M2: Semantic layer ─────────────
  /** DynamoDB semantic system-of-record 테이블명 (agentic-t2sql-semantic) */
  readonly semanticTableName: string;
  /** semantic 문서용 OpenSearch 인덱스명 (t2sql-semantic — OSIS 싱크 대상) */
  readonly semanticIndex: string;
  /** Neptune Serverless 최소 용량 (NCU, 최소 1.0) */
  readonly graphMinNcu: number;
  /** Neptune Serverless 최대 용량 (NCU) */
  readonly graphMaxNcu: number;

  // ───────────── M3: Gateway / Identity / Policy(Cedar) ─────────────
  /** Gateway 이름 (agentic-t2sql-gateway). 하이픈 허용 패턴 ^([0-9a-zA-Z][-]?){1,48}$ */
  readonly gatewayName: string;
  /** PolicyEngine 이름. ⚠️ 패턴 ^[A-Za-z][A-Za-z0-9_]*$ — 하이픈 불가, 언더스코어만. */
  readonly policyEngineName: string;
  /** Gateway MCP target 이름(도구 이름 prefix 가 됨: `<target>___<tool>`). */
  readonly sqlTargetName: string;
  readonly semanticTargetName: string;
  /**
   * 오케스트레이터 도구 평면 모드: "direct"(기본, Runtime MCP 직접 SigV4) | "gateway"(Gateway 집약).
   * Gateway 배포 후 update-agent-runtime 으로 "gateway" 전환한다(순환 의존 방지).
   */
  readonly toolPlaneMode: string;
  /**
   * Gateway MCP 엔드포인트 URL. runtime→gateway 참조가 순환이므로 배포 시점엔 알 수 없다.
   * 기본 '' 로 두고 gateway 배포 후 update-agent-runtime 으로 주입한다.
   */
  readonly gatewayUrl: string;

  // ───────────── M3: Redshift Serverless (Data Layer 2번째 소스) ─────────────
  /** Redshift Serverless namespace 이름 (agentic-t2sql-rs-ns). */
  readonly redshiftNamespaceName: string;
  /** Redshift Serverless workgroup 이름 (agentic-t2sql-rs-wg). */
  readonly redshiftWorkgroupName: string;
  /**
   * Redshift base capacity (RPU). 4 RPU 최소(us-west-2/Oregon 는 4 RPU 런치 리전) — 데모 비용 최소.
   * 배포 시 4 가 거부되면 8 로 상향(context redshiftBaseCapacity=8).
   */
  readonly redshiftBaseCapacity: number;
  /** Redshift read-only 사용자(agent_ro) 시크릿 이름. */
  readonly redshiftRoSecretName: string;

  // ───────────── M4: Admin panel / datasource-admin-mcp ─────────────
  /** Gateway MCP target 이름 (도구명 prefix `datasource-admin-mcp___<tool>`). */
  readonly adminMcpTargetName: string;
  /**
   * Cedar action 스코프 2-phase 스위치 (기본 false).
   *
   * M3 학습: `action in [AgentCore::Action::"<Target>___<tool>"]` 스코프는 target 의 도구
   * 동기화가 끝나기 전에 정책이 검증되어 "unable to find an applicable action" 으로 실패한다.
   * 따라서 (phase 1) false 로 admin target 을 포함한 gateway 를 먼저 배포하고,
   * (phase 2) `-c cedarActionScoping=true` 로 재배포해 정책 statement 만 좁힌다.
   */
  readonly cedarActionScoping: boolean;
}

const DEFAULTS = {
  appPrefix: 'agentic-t2sql',
  region: 'us-west-2',
  dbName: 'ecommerce',
  readOnlyDbUser: 'agent_ro',
  openSearchIndex: 't2sql-schema-docs',
  embeddingModelId: 'amazon.titan-embed-text-v2:0',
  modelId: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
  ecrRepos: {
    orchestrator: 'agentic-t2sql/orchestrator',
    sqlExecutionMcp: 'agentic-t2sql/sql-execution-mcp',
    semanticRetrievalMcp: 'agentic-t2sql/semantic-retrieval-mcp',
    ui: 'agentic-t2sql/ui',
    // M4 admin panel
    datasourceAdminMcp: 'agentic-t2sql/datasource-admin-mcp',
    adminWeb: 'agentic-t2sql/admin-web',
  },
  // M2 semantic layer
  semanticTableName: 'agentic-t2sql-semantic',
  semanticIndex: 't2sql-semantic',
  graphMinNcu: 1,
  graphMaxNcu: 2.5,
  // M3 gateway / policy
  gatewayName: 'agentic-t2sql-gateway',
  // PolicyEngine·Policy 이름은 언더스코어만 허용(하이픈 불가) → prefix 의 하이픈을 치환.
  policyEngineName: 'agentic_t2sql_policy_engine',
  sqlTargetName: 'sql-execution-mcp',
  semanticTargetName: 'semantic-retrieval-mcp',
  toolPlaneMode: 'direct',
  gatewayUrl: '',
  // M3 redshift
  redshiftNamespaceName: 'agentic-t2sql-rs-ns',
  redshiftWorkgroupName: 'agentic-t2sql-rs-wg',
  redshiftBaseCapacity: 4,
  redshiftRoSecretName: 'agentic-t2sql/redshift/agent_ro',
  // M4 admin panel
  adminMcpTargetName: 'datasource-admin-mcp',
  cedarActionScoping: false,
} as const;

/**
 * cdk.json context 를 읽어 AppConfig 를 구성한다. context 에 값이 없으면
 * 위 DEFAULTS 로 폴백해 로컬 synth 가 항상 동작하도록 한다.
 */
export function loadConfig(scope: Construct): AppConfig {
  const ctx = scope.node.tryGetContext.bind(scope.node);
  const ecr = (ctx('ecrRepos') as Partial<EcrRepoNames> | undefined) ?? {};
  return {
    appPrefix: ctx('appPrefix') ?? DEFAULTS.appPrefix,
    region: ctx('region') ?? DEFAULTS.region,
    dbName: ctx('dbName') ?? DEFAULTS.dbName,
    readOnlyDbUser: ctx('readOnlyDbUser') ?? DEFAULTS.readOnlyDbUser,
    openSearchIndex: ctx('openSearchIndex') ?? DEFAULTS.openSearchIndex,
    embeddingModelId: ctx('embeddingModelId') ?? DEFAULTS.embeddingModelId,
    modelId: ctx('modelId') ?? DEFAULTS.modelId,
    ecrRepos: {
      orchestrator: ecr.orchestrator ?? DEFAULTS.ecrRepos.orchestrator,
      sqlExecutionMcp: ecr.sqlExecutionMcp ?? DEFAULTS.ecrRepos.sqlExecutionMcp,
      semanticRetrievalMcp:
        ecr.semanticRetrievalMcp ?? DEFAULTS.ecrRepos.semanticRetrievalMcp,
      ui: ecr.ui ?? DEFAULTS.ecrRepos.ui,
      datasourceAdminMcp: ecr.datasourceAdminMcp ?? DEFAULTS.ecrRepos.datasourceAdminMcp,
      adminWeb: ecr.adminWeb ?? DEFAULTS.ecrRepos.adminWeb,
    },
    semanticTableName: ctx('semanticTableName') ?? DEFAULTS.semanticTableName,
    semanticIndex: ctx('semanticIndex') ?? DEFAULTS.semanticIndex,
    graphMinNcu: (ctx('graphMinNcu') as number | undefined) ?? DEFAULTS.graphMinNcu,
    graphMaxNcu: (ctx('graphMaxNcu') as number | undefined) ?? DEFAULTS.graphMaxNcu,
    // M3 gateway / policy
    gatewayName: ctx('gatewayName') ?? DEFAULTS.gatewayName,
    policyEngineName: ctx('policyEngineName') ?? DEFAULTS.policyEngineName,
    sqlTargetName: ctx('sqlTargetName') ?? DEFAULTS.sqlTargetName,
    semanticTargetName: ctx('semanticTargetName') ?? DEFAULTS.semanticTargetName,
    toolPlaneMode: ctx('toolPlaneMode') ?? DEFAULTS.toolPlaneMode,
    gatewayUrl: ctx('gatewayUrl') ?? DEFAULTS.gatewayUrl,
    // M3 redshift
    redshiftNamespaceName: ctx('redshiftNamespaceName') ?? DEFAULTS.redshiftNamespaceName,
    redshiftWorkgroupName: ctx('redshiftWorkgroupName') ?? DEFAULTS.redshiftWorkgroupName,
    redshiftBaseCapacity:
      (ctx('redshiftBaseCapacity') as number | undefined) ?? DEFAULTS.redshiftBaseCapacity,
    redshiftRoSecretName: ctx('redshiftRoSecretName') ?? DEFAULTS.redshiftRoSecretName,
    // M4 admin panel
    adminMcpTargetName: ctx('adminMcpTargetName') ?? DEFAULTS.adminMcpTargetName,
    // CLI `-c cedarActionScoping=true` 는 문자열로 전달되므로 'true' 도 받아들인다
    // (cdk.json context 에 boolean 으로 두는 경우도 지원).
    cedarActionScoping: parseBool(ctx('cedarActionScoping'), DEFAULTS.cedarActionScoping),
  };
}

/** context 값을 boolean 으로 정규화한다(문자열 'true'/'1' 도 true 로 취급). */
function parseBool(value: unknown, fallback: boolean): boolean {
  if (value === undefined || value === null) return fallback;
  if (typeof value === 'boolean') return value;
  return ['true', '1', 'yes'].includes(String(value).toLowerCase());
}
