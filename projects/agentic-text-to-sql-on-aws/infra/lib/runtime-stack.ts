import {
  Stack,
  StackProps,
  CfnOutput,
  aws_ec2 as ec2,
  aws_rds as rds,
  aws_ecr as ecr,
  aws_iam as iam,
  aws_opensearchservice as opensearch,
  aws_secretsmanager as secretsmanager,
  aws_bedrockagentcore as agentcore,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { AppConfig } from './config';

export interface RuntimeStackProps extends StackProps {
  readonly config: AppConfig;
  // base 스택에서 전달받는 참조들(props 로 명시 전달 — cross-stack export 자동 생성)
  readonly auroraCluster: rds.DatabaseCluster;
  readonly agentRoSecret: secretsmanager.ISecret;
  // M3: Redshift 2번째 소스. base 가 소유하므로 object 참조로 전달(결정적 cross-stack export).
  readonly redshiftRoSecret: secretsmanager.ISecret;
  readonly openSearchDomain: opensearch.IDomain;
  readonly memory: agentcore.Memory;
  readonly ecrOrchestrator: ecr.IRepository;
  readonly ecrSqlMcp: ecr.IRepository;
  readonly ecrSemanticMcp: ecr.IRepository;
  /** M4: datasource-admin-mcp 이미지 리포 */
  readonly ecrAdminMcp: ecr.IRepository;
  readonly orchestratorRole: iam.Role;
  readonly sqlMcpRole: iam.Role;
  readonly semanticMcpRole: iam.Role;
  /** M4: datasource-admin-mcp Runtime 실행 role (base 소유) */
  readonly adminMcpRole: iam.Role;
  // M2: semantic 스택에서 전달받는 참조들 (Neptune VPC 접근)
  readonly vpc: ec2.IVpc;
  /** Neptune 전용 SG — semantic MCP runtime 로부터 8182 인바운드를 여기서 허용 */
  readonly graphSecurityGroup: ec2.SecurityGroup;
  /** Neptune HTTPS 엔드포인트 (semantic MCP env 주입) */
  readonly graphEndpoint: string;
}

/**
 * AgenticT2SqlRuntimeStack — AgentCore Runtime 4개.
 *
 *  - sql-execution-mcp   : protocol MCP  (port 8000, /mcp)
 *  - semantic-retrieval-mcp: protocol MCP (port 8000, /mcp)
 *  - datasource-admin-mcp: protocol MCP (port 8000, /mcp) — M4 관리 도구 평면
 *  - orchestrator        : protocol HTTP (port 8080, /invocations, /ping)
 *
 * 모두 fromEcrRepository(repo, 'latest') 로 이미지 참조(D9: direct code upload 금지).
 * 이미지가 ECR 에 push 된 뒤에 배포돼야 하므로 base 스택 → 이미지 push → 이 스택 순서.
 */
export class AgenticT2SqlRuntimeStack extends Stack {
  public readonly orchestratorRuntime: agentcore.Runtime;
  public readonly sqlMcpRuntime: agentcore.Runtime;
  public readonly semanticMcpRuntime: agentcore.Runtime;
  /** M4: 관리 도구(큐레이션·승인·데이터소스) MCP runtime */
  public readonly adminMcpRuntime: agentcore.Runtime;

  constructor(scope: Construct, id: string, props: RuntimeStackProps) {
    super(scope, id, props);
    const { config } = props;
    const nameBase = config.appPrefix.replace(/-/g, '_');

    // ───────────────── SQL execution MCP runtime (MCP protocol) ─────────────────
    this.sqlMcpRuntime = new agentcore.Runtime(this, 'SqlMcpRuntime', {
      runtimeName: `${nameBase}_sql_execution_mcp`,
      description: 'SQL validation (SQLGlot AST allow-list) + execution via RDS Data API',
      agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromEcrRepository(
        props.ecrSqlMcp,
        'latest',
      ),
      protocolConfiguration: agentcore.ProtocolType.MCP,
      executionRole: props.sqlMcpRole,
      networkConfiguration: agentcore.RuntimeNetworkConfiguration.usingPublicNetwork(),
      tracingEnabled: true,
      environmentVariables: {
        AURORA_CLUSTER_ARN: props.auroraCluster.clusterArn,
        AURORA_SECRET_ARN: props.agentRoSecret.secretArn,
        DB_NAME: config.dbName,
        // M3: 2번째 데이터 소스(datasource="redshift"). base 가 결정적으로 전달(순환 없음).
        REDSHIFT_WORKGROUP: config.redshiftWorkgroupName,
        REDSHIFT_DB: config.dbName,
        REDSHIFT_SECRET_ARN: props.redshiftRoSecret.secretArn,
      },
    });

    // ───────────────── Semantic retrieval MCP runtime (MCP protocol) ─────────────────
    // M2: VPC 모드 전환. Neptune 은 VPC 내부(PRIVATE_ISOLATED)라 runtime 이 VPC 안에서 접근하고,
    // OpenSearch 퍼블릭 엔드포인트는 PRIVATE_WITH_EGRESS 서브넷의 NAT 로 나간다.
    //
    // semantic runtime 전용 SG 를 이 스택에서 만들어 runtime 에 붙이고, Neptune SG 인바운드
    // 규칙도 이 스택에 배치한다(runtime→semantic 단방향 의존이므로 규칙 리소스는 runtime 에 둬야
    // 사이클이 안 생긴다). Neptune SG 를 mutable 로 import 하여 ingress 규칙만 이쪽에 렌더한다.
    const semanticRuntimeSg = new ec2.SecurityGroup(this, 'SemanticRuntimeSg', {
      vpc: props.vpc,
      securityGroupName: `${config.appPrefix}-semantic-runtime-sg`,
      description: 'Semantic MCP runtime to Neptune/OpenSearch outbound',
      allowAllOutbound: true,
    });
    ec2.SecurityGroup.fromSecurityGroupId(
      this,
      'GraphSgRef',
      props.graphSecurityGroup.securityGroupId,
      { mutable: true },
    ).addIngressRule(
      ec2.Peer.securityGroupId(semanticRuntimeSg.securityGroupId),
      ec2.Port.tcp(8182),
      'semantic MCP runtime to Neptune 8182',
    );
    this.semanticMcpRuntime = new agentcore.Runtime(this, 'SemanticMcpRuntime', {
      runtimeName: `${nameBase}_semantic_retrieval_mcp`,
      description: 'Schema/term/synonym/few-shot retrieval via OpenSearch hybrid + Neptune graph 순회',
      agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromEcrRepository(
        props.ecrSemanticMcp,
        'latest',
      ),
      protocolConfiguration: agentcore.ProtocolType.MCP,
      executionRole: props.semanticMcpRole,
      networkConfiguration: agentcore.RuntimeNetworkConfiguration.usingVpc(this, {
        vpc: props.vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        securityGroups: [semanticRuntimeSg],
      }),
      tracingEnabled: true,
      environmentVariables: {
        OPENSEARCH_ENDPOINT: `https://${props.openSearchDomain.domainEndpoint}`,
        OPENSEARCH_INDEX: config.openSearchIndex,
        EMBEDDING_MODEL_ID: config.embeddingModelId,
        // 관리형 OpenSearch 도메인이므로 SigV4 서명 서비스명은 "es"
        // (seed 인덱서와 정합; Serverless 로 전환 시 "aoss").
        OPENSEARCH_SERVICE: 'es',
        // M2: Neptune 그래프 순회 + semantic 인덱스. SEMANTIC_GRAPH_ENABLED=true 로
        // GraphAugmentedRetriever 활성(미배포 환경에선 false 로 OpenSearch 단독 graceful degrade).
        GRAPH_ENDPOINT: props.graphEndpoint,
        SEMANTIC_GRAPH_ENABLED: 'true',
        SEMANTIC_INDEX: config.semanticIndex,
      },
    });

    // ───────────────── Datasource admin MCP runtime (MCP protocol, M4) ─────────────────
    // semantic 큐레이션(CRUD·publish)·데이터소스 등록/테스트·스키마 크롤 도구를 제공한다.
    // DynamoDB(semantic)·Data API·Secrets Manager 는 모두 AWS API 평면이라 VPC 불필요 →
    // PUBLIC 네트워크(sql-mcp 와 동형). 인가는 Gateway 앞단의 Cedar 가 담당한다(§8.0).
    this.adminMcpRuntime = new agentcore.Runtime(this, 'AdminMcpRuntime', {
      runtimeName: `${nameBase}_datasource_admin_mcp`,
      description: 'Semantic 큐레이션·승인 + 데이터소스 등록/테스트/스키마 크롤 MCP (admin panel 도구 평면)',
      agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromEcrRepository(
        props.ecrAdminMcp,
        'latest',
      ),
      protocolConfiguration: agentcore.ProtocolType.MCP,
      executionRole: props.adminMcpRole,
      networkConfiguration: agentcore.RuntimeNetworkConfiguration.usingPublicNetwork(),
      tracingEnabled: true,
      environmentVariables: {
        // semantic 쓰기 단일 지점(DynamoDB). 테이블명은 config 리터럴 — base role 의 ARN 과 정합.
        SEMANTIC_TABLE_NAME: config.semanticTableName,
        EMBEDDING_MODEL_ID: config.embeddingModelId,
        // 스키마 크롤용 데이터 소스 접속 정보(read-only 자격증명).
        AURORA_CLUSTER_ARN: props.auroraCluster.clusterArn,
        AURORA_SECRET_ARN: props.agentRoSecret.secretArn,
        DB_NAME: config.dbName,
        REDSHIFT_WORKGROUP: config.redshiftWorkgroupName,
        REDSHIFT_DB: config.dbName,
        REDSHIFT_SECRET_ARN: props.redshiftRoSecret.secretArn,
        // 등록 데이터소스 자격증명 시크릿 프리픽스(role 정책의 리소스 제한과 반드시 일치).
        DATASOURCE_SECRET_PREFIX: `${config.appPrefix}/datasource/`,
        // M5 Track B: 후보 채굴기(mine_candidates)가 orchestrator 의 `t2sql_query_record`
        // 로그를 찾을 프리픽스. adminMcpRole 의 logs 리소스 제한과 일치해야 한다.
        ORCHESTRATOR_LOG_GROUP_PREFIX: '/aws/bedrock-agentcore/runtimes/',
      },
    });

    // ───────────────── Orchestrator runtime (AG-UI protocol) ─────────────────
    // orchestrator 는 ag_ui_strands + BedrockAgentCoreApp 로 AG-UI SSE 를 서빙한다.
    // AG-UI 는 HTTP 와 동일한 포트 8080 / POST /invocations(SSE), GET /ping 를 쓰지만
    // 런타임이 --protocol(=serverProtocol) 플래그로 HTTP 와 구분한다. AG-UI 로 지정하면
    // AgentCore 가 AG-UI 이벤트 스트림(TEXT_MESSAGE_*, TOOL_CALL_*, STATE_* 등)을 프록시하고
    // 세션 격리·인증(SigV4/OAuth)을 처리한다. AG-UI 에이전트의 올바른 설정값.
    // (근거: docs runtime-agui.html / runtime-service-contract.html — serverProtocol enum: MCP|HTTP|A2A|AGUI)
    // MCP runtime ARN 을 orchestrator env 로 주입(도구 호출 대상). 순서 의존이 명시된다.
    this.orchestratorRuntime = new agentcore.Runtime(this, 'OrchestratorRuntime', {
      runtimeName: `${nameBase}_orchestrator`,
      description: 'Strands Graph orchestrator: NL → schema-link → SQL → validate → execute (AG-UI SSE)',
      agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromEcrRepository(
        props.ecrOrchestrator,
        'latest',
      ),
      protocolConfiguration: agentcore.ProtocolType.AGUI,
      executionRole: props.orchestratorRole,
      networkConfiguration: agentcore.RuntimeNetworkConfiguration.usingPublicNetwork(),
      tracingEnabled: true,
      environmentVariables: {
        SQL_MCP_ARN: this.sqlMcpRuntime.agentRuntimeArn,
        SEMANTIC_MCP_ARN: this.semanticMcpRuntime.agentRuntimeArn,
        MEMORY_ID: props.memory.memoryId,
        MODEL_ID: config.modelId,
        // ───────── M3: 도구 평면(tool plane) 모드 ─────────
        // 기본 "direct"(Runtime MCP 직접 SigV4). Gateway 배포는 이 스택 '이후'이므로 runtime→gateway
        // 참조는 순환이다. 따라서 GATEWAY_URL 은 결정적으로 알 수 없어 placeholder(빈 문자열/컨텍스트)로
        // 두고, gateway 배포 후 `aws bedrock-agentcore update-agent-runtime` 로 두 env 를 주입해
        // "gateway" 로 전환한다(CLAUDE.md M2 학습: 이미지/env 갱신은 update-agent-runtime). gateway
        // 모드의 Cognito M2M env(COGNITO_*)도 그 시점에 함께 주입한다.
        TOOL_PLANE_MODE: config.toolPlaneMode,
        GATEWAY_URL: config.gatewayUrl,
        // ───────── M5: bundle 기반 프롬프트/모델 오버라이드 + 버전 스탬프(§9.5) ─────────
        // 활성 bundle 포인터(SSM). 파라미터 자체는 evaluation 스택 소유이고 runtime→evaluation
        // 참조는 배포 순서상 역방향이므로, 값은 config 리터럴(이름 규칙)로 주입한다.
        // 파라미터가 없거나 빈 값이면 orchestrator 는 코드 기본값으로 폴백한다(AGENTREL04).
        CONFIG_BUNDLE_PARAM: config.activeBundleParamName,
        // t2sql_query_record 의 version.agent 스탬프(평가·채굴 결과의 버전 귀인).
        APP_VERSION: config.appVersion,
      },
    });

    // ───────────────────────── Outputs ─────────────────────────
    new CfnOutput(this, 'OrchestratorRuntimeArn', {
      value: this.orchestratorRuntime.agentRuntimeArn,
      description: 'Orchestrator AgentCore Runtime ARN (invoked by the UI)',
      exportName: `${config.appPrefix}-orchestrator-runtime-arn`,
    });
    new CfnOutput(this, 'SqlMcpRuntimeArn', {
      value: this.sqlMcpRuntime.agentRuntimeArn,
      description: 'SQL execution MCP runtime ARN',
    });
    new CfnOutput(this, 'SemanticMcpRuntimeArn', {
      value: this.semanticMcpRuntime.agentRuntimeArn,
      description: 'Semantic retrieval MCP runtime ARN',
    });
    new CfnOutput(this, 'AdminMcpRuntimeArn', {
      value: this.adminMcpRuntime.agentRuntimeArn,
      description: 'Datasource admin MCP runtime ARN (M4 — gateway target)',
    });
  }
}
