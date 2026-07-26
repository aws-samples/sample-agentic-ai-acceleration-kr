import {
  Stack,
  StackProps,
  CfnOutput,
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
  readonly openSearchDomain: opensearch.IDomain;
  readonly memory: agentcore.Memory;
  readonly ecrOrchestrator: ecr.IRepository;
  readonly ecrSqlMcp: ecr.IRepository;
  readonly ecrSemanticMcp: ecr.IRepository;
  readonly orchestratorRole: iam.Role;
  readonly sqlMcpRole: iam.Role;
  readonly semanticMcpRole: iam.Role;
}

/**
 * AgenticT2SqlRuntimeStack — AgentCore Runtime 3개.
 *
 *  - sql-execution-mcp   : protocol MCP  (port 8000, /mcp)
 *  - semantic-retrieval-mcp: protocol MCP (port 8000, /mcp)
 *  - orchestrator        : protocol HTTP (port 8080, /invocations, /ping)
 *
 * 모두 fromEcrRepository(repo, 'latest') 로 이미지 참조(D9: direct code upload 금지).
 * 이미지가 ECR 에 push 된 뒤에 배포돼야 하므로 base 스택 → 이미지 push → 이 스택 순서.
 */
export class AgenticT2SqlRuntimeStack extends Stack {
  public readonly orchestratorRuntime: agentcore.Runtime;
  public readonly sqlMcpRuntime: agentcore.Runtime;
  public readonly semanticMcpRuntime: agentcore.Runtime;

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
      },
    });

    // ───────────────── Semantic retrieval MCP runtime (MCP protocol) ─────────────────
    this.semanticMcpRuntime = new agentcore.Runtime(this, 'SemanticMcpRuntime', {
      runtimeName: `${nameBase}_semantic_retrieval_mcp`,
      description: 'Schema/term/synonym/few-shot retrieval via OpenSearch hybrid search',
      agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromEcrRepository(
        props.ecrSemanticMcp,
        'latest',
      ),
      protocolConfiguration: agentcore.ProtocolType.MCP,
      executionRole: props.semanticMcpRole,
      networkConfiguration: agentcore.RuntimeNetworkConfiguration.usingPublicNetwork(),
      tracingEnabled: true,
      environmentVariables: {
        OPENSEARCH_ENDPOINT: `https://${props.openSearchDomain.domainEndpoint}`,
        OPENSEARCH_INDEX: config.openSearchIndex,
        EMBEDDING_MODEL_ID: config.embeddingModelId,
        // 관리형 OpenSearch 도메인이므로 SigV4 서명 서비스명은 "es"
        // (seed 인덱서와 정합; Serverless 로 전환 시 "aoss").
        OPENSEARCH_SERVICE: 'es',
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
  }
}
