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
  },
  // M2 semantic layer
  semanticTableName: 'agentic-t2sql-semantic',
  semanticIndex: 't2sql-semantic',
  graphMinNcu: 1,
  graphMaxNcu: 2.5,
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
    },
    semanticTableName: ctx('semanticTableName') ?? DEFAULTS.semanticTableName,
    semanticIndex: ctx('semanticIndex') ?? DEFAULTS.semanticIndex,
    graphMinNcu: (ctx('graphMinNcu') as number | undefined) ?? DEFAULTS.graphMinNcu,
    graphMaxNcu: (ctx('graphMaxNcu') as number | undefined) ?? DEFAULTS.graphMaxNcu,
  };
}
