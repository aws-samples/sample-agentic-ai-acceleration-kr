import {
  Stack,
  StackProps,
  RemovalPolicy,
  Duration,
  CfnOutput,
  aws_ec2 as ec2,
  aws_rds as rds,
  aws_ecr as ecr,
  aws_iam as iam,
  aws_cognito as cognito,
  aws_opensearchservice as opensearch,
  aws_secretsmanager as secretsmanager,
  aws_bedrockagentcore as agentcore,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { AppConfig } from './config';

export interface BaseStackProps extends StackProps {
  readonly config: AppConfig;
}

/**
 * AgenticT2SqlBaseStack — 다른 스택보다 먼저 배포되어야 하는 기반 리소스.
 *
 * 포함: VPC, Aurora Serverless v2(+Data API), OpenSearch 도메인, ECR 리포 4개,
 * Cognito(user pool + client + groups), AgentCore Memory(STM),
 * 컴포넌트별 IAM role(orchestrator / sql-mcp / semantic-mcp / ui — 최소 권한 분리).
 *
 * Runtime/UI 스택은 여기서 만든 리소스를 props(객체 참조)로 전달받는다.
 */
export class AgenticT2SqlBaseStack extends Stack {
  // Runtime/UI 스택으로 전달할 참조들
  public readonly vpc: ec2.IVpc;
  public readonly auroraCluster: rds.DatabaseCluster;
  public readonly agentRoSecret: secretsmanager.ISecret;
  public readonly openSearchDomain: opensearch.IDomain;
  public readonly memory: agentcore.Memory;

  public readonly ecrOrchestrator: ecr.IRepository;
  public readonly ecrSqlMcp: ecr.IRepository;
  public readonly ecrSemanticMcp: ecr.IRepository;
  public readonly ecrUi: ecr.IRepository;

  public readonly orchestratorRole: iam.Role;
  public readonly sqlMcpRole: iam.Role;
  public readonly semanticMcpRole: iam.Role;
  public readonly uiTaskRole: iam.Role;

  public readonly userPool: cognito.UserPool;
  public readonly userPoolClient: cognito.UserPoolClient;

  constructor(scope: Construct, id: string, props: BaseStackProps) {
    super(scope, id, props);
    const { config } = props;
    const prefix = config.appPrefix;

    // ───────────────────────── VPC (NAT 1개, 최소 비용) ─────────────────────────
    // Aurora 는 private isolated 서브넷(Data API 는 공용 AWS 엔드포인트라 아웃바운드 불필요),
    // UI(ECS Fargate)는 private-with-egress 에 배치. AZ 2개, NAT 1개로 비용 최소화.
    this.vpc = new ec2.Vpc(this, 'Vpc', {
      vpcName: `${prefix}-vpc`,
      maxAzs: 2,
      natGateways: 1,
      subnetConfiguration: [
        { name: 'public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
        {
          name: 'private',
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
          cidrMask: 24,
        },
        {
          name: 'isolated',
          subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
          cidrMask: 24,
        },
      ],
    });

    // ───────────────────────── read-only DB 사용자 시크릿 ─────────────────────────
    // Aurora 마스터 시크릿은 클러스터가 자동 생성. 여기서는 read-only 사용자(agent_ro)의
    // 시크릿을 별도 생성한다. seed 스크립트(Task #2/#6)가 이 자격증명으로 DB 사용자를
    // 생성하고 SELECT-only 권한을 부여한다. MCP 서버는 이 시크릿만 읽는다(READ-ONLY 4중 방어 중 하나).
    this.agentRoSecret = new secretsmanager.Secret(this, 'AgentRoSecret', {
      secretName: `${prefix}/aurora/agent_ro`,
      description: 'Read-only DB user credentials for the SQL execution MCP (Data API)',
      generateSecretString: {
        secretStringTemplate: JSON.stringify({ username: config.readOnlyDbUser }),
        generateStringKey: 'password',
        excludePunctuation: true,
        passwordLength: 32,
      },
    });

    // ───────────────────────── Aurora Serverless v2 + Data API ─────────────────────────
    this.auroraCluster = new rds.DatabaseCluster(this, 'Aurora', {
      clusterIdentifier: `${prefix}-aurora`,
      engine: rds.DatabaseClusterEngine.auroraPostgres({
        // 16.6은 us-west-2에서 제공 종료됨(2026-07 기준 16.8+만 제공) — 리전 제공 버전과
        // CDK 상수가 모두 존재하는 16.9 사용
        version: rds.AuroraPostgresEngineVersion.VER_16_9,
      }),
      writer: rds.ClusterInstance.serverlessV2('writer'),
      serverlessV2MinCapacity: 0.5,
      serverlessV2MaxCapacity: 2,
      enableDataApi: true,
      defaultDatabaseName: config.dbName,
      credentials: rds.Credentials.fromGeneratedSecret('postgres', {
        secretName: `${prefix}/aurora/master`,
      }),
      vpc: this.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      storageEncrypted: true,
      // 샘플/데모 환경: 스택 삭제 시 정리. 프로덕션에서는 RETAIN + 삭제 방지 검토.
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // ───────────────────────── OpenSearch 관리형 도메인 (최소형) ─────────────────────────
    // AgentCore Runtime 은 PUBLIC 네트워크 모드이므로 MCP 서버가 도메인에 접근하려면
    // 퍼블릭 엔드포인트 + IAM(SigV4) 접근 제어가 필요하다(VPC 배치 금지).
    // access policy 로 semantic-mcp role 과 계정 principal(seed 스크립트용)만 허용.
    this.openSearchDomain = new opensearch.Domain(this, 'OpenSearch', {
      domainName: `${prefix}-search`,
      version: opensearch.EngineVersion.OPENSEARCH_2_17,
      capacity: {
        dataNodes: 1,
        dataNodeInstanceType: 't3.small.search',
        multiAzWithStandbyEnabled: false,
      },
      ebs: {
        volumeSize: 10,
        volumeType: ec2.EbsDeviceVolumeType.GP3,
      },
      zoneAwareness: { enabled: false },
      enforceHttps: true,
      nodeToNodeEncryption: true,
      encryptionAtRest: { enabled: true },
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // ───────────────────────── ECR 리포 4개 ─────────────────────────
    const makeRepo = (idSuffix: string, repoName: string): ecr.Repository =>
      new ecr.Repository(this, `Ecr${idSuffix}`, {
        repositoryName: repoName,
        imageScanOnPush: true,
        imageTagMutability: ecr.TagMutability.MUTABLE,
        emptyOnDelete: true,
        removalPolicy: RemovalPolicy.DESTROY,
        lifecycleRules: [{ maxImageCount: 10 }],
      });
    this.ecrOrchestrator = makeRepo('Orchestrator', config.ecrRepos.orchestrator);
    this.ecrSqlMcp = makeRepo('SqlMcp', config.ecrRepos.sqlExecutionMcp);
    this.ecrSemanticMcp = makeRepo('SemanticMcp', config.ecrRepos.semanticRetrievalMcp);
    this.ecrUi = makeRepo('Ui', config.ecrRepos.ui);

    // ───────────────────────── Cognito user pool + client + groups ─────────────────────────
    // M1 은 프로비저닝만. Admin/Manager 그룹은 admin panel 페르소나 분리를 위한 골격.
    this.userPool = new cognito.UserPool(this, 'UserPool', {
      userPoolName: `${prefix}-users`,
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      standardAttributes: { email: { required: true, mutable: true } },
      passwordPolicy: {
        minLength: 12,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    this.userPoolClient = this.userPool.addClient('WebClient', {
      userPoolClientName: `${prefix}-web-client`,
      generateSecret: false,
      authFlows: { userSrp: true, userPassword: false },
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
      },
    });
    new cognito.CfnUserPoolGroup(this, 'AdminGroup', {
      userPoolId: this.userPool.userPoolId,
      groupName: 'Admin',
      description: '관리자: 인프라 운영, 데이터 소스 연결, 권한/상태 관리',
    });
    new cognito.CfnUserPoolGroup(this, 'ManagerGroup', {
      userPoolId: this.userPool.userPoolId,
      groupName: 'Manager',
      description: '매니저: semantic 큐레이션, 평가 리뷰/승인',
    });

    // ───────────────────────── AgentCore Memory (STM only, M1) ─────────────────────────
    // 장기 메모리 전략(LTM)은 M1 범위 밖. STM raw event 보존 30일.
    this.memory = new agentcore.Memory(this, 'Memory', {
      memoryName: `${prefix.replace(/-/g, '_')}_memory`,
      description: 'Short-term conversation memory for the orchestrator agent',
      expirationDuration: Duration.days(30),
    });

    // ───────────────────────── 컴포넌트별 IAM role (최소 권한 분리) ─────────────────────────
    // AgentCore Runtime 실행 role 3종. trust: bedrock-agentcore.amazonaws.com +
    // aws:SourceAccount 조건(confused deputy 방어). Runtime L2 가 로그/X-Ray/메트릭/
    // workload-identity 권한을 자동 부가하므로 여기서는 각 컴포넌트 고유 권한만 부여.
    const agentCorePrincipal = new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
      conditions: {
        StringEquals: { 'aws:SourceAccount': this.account },
      },
    });

    // sql-execution-mcp: Data API(rds-data) + agent_ro 시크릿 읽기만
    this.sqlMcpRole = new iam.Role(this, 'SqlMcpRole', {
      roleName: `${prefix}-sql-mcp-role`,
      assumedBy: agentCorePrincipal,
      description: 'Execution role for the SQL execution MCP runtime',
    });
    this.sqlMcpRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'DataApiExecute',
        actions: [
          'rds-data:ExecuteStatement',
          'rds-data:BatchExecuteStatement',
          'rds-data:BeginTransaction',
          'rds-data:CommitTransaction',
          'rds-data:RollbackTransaction',
        ],
        resources: [this.auroraCluster.clusterArn],
      }),
    );
    this.agentRoSecret.grantRead(this.sqlMcpRole);

    // semantic-retrieval-mcp: OpenSearch es:ESHttpGet/Post(해당 도메인) + Titan embed 호출만
    this.semanticMcpRole = new iam.Role(this, 'SemanticMcpRole', {
      roleName: `${prefix}-semantic-mcp-role`,
      assumedBy: agentCorePrincipal,
      description: 'Execution role for the semantic retrieval MCP runtime',
    });
    this.semanticMcpRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'OpenSearchHttp',
        actions: ['es:ESHttpGet', 'es:ESHttpPost', 'es:ESHttpPut', 'es:ESHttpHead'],
        resources: [this.openSearchDomain.domainArn, `${this.openSearchDomain.domainArn}/*`],
      }),
    );
    this.semanticMcpRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'BedrockTitanEmbed',
        actions: ['bedrock:InvokeModel'],
        resources: [
          `arn:${this.partition}:bedrock:${this.region}::foundation-model/${config.embeddingModelId}`,
        ],
      }),
    );

    // orchestrator: Bedrock 모델 호출 + MCP runtime 2개 호출 + Memory 접근
    // (InvokeAgentRuntime 대상 ARN 은 순환 의존을 피하려 계정/리전 스코프 와일드카드로 제한)
    this.orchestratorRole = new iam.Role(this, 'OrchestratorRole', {
      roleName: `${prefix}-orchestrator-role`,
      assumedBy: agentCorePrincipal,
      description: 'Execution role for the orchestrator agent runtime',
    });
    this.orchestratorRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'BedrockInvokeModels',
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [
          `arn:${this.partition}:bedrock:${this.region}::foundation-model/*`,
          `arn:${this.partition}:bedrock:${this.region}:${this.account}:inference-profile/*`,
          `arn:${this.partition}:bedrock:*::foundation-model/*`,
        ],
      }),
    );
    this.orchestratorRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'InvokeMcpRuntimes',
        actions: ['bedrock-agentcore:InvokeAgentRuntime'],
        resources: [
          `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:runtime/${prefix.replace(/-/g, '_')}_sql_execution_mcp*`,
          `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:runtime/${prefix.replace(/-/g, '_')}_semantic_retrieval_mcp*`,
        ],
      }),
    );
    // Memory 접근. M1 은 STM 만이므로 event 쓰기 + STM 읽기 액션만 부여(LTM record 액션 제외).
    this.memory.grantWrite(this.orchestratorRole);
    this.memory.grantReadShortTermMemory(this.orchestratorRole);

    // ui: ECS task role. orchestrator runtime 호출만.
    // 순환 의존을 피하려 runtime 스택의 출력(ARN 토큰)을 참조하지 않고,
    // 결정적 runtime 이름 규칙으로 ARN 을 직접 구성한다(orchestrator→MCP grant 와 동일 패턴).
    this.uiTaskRole = new iam.Role(this, 'UiTaskRole', {
      roleName: `${prefix}-ui-task-role`,
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description: 'Task role for the UI Fargate service',
    });
    const orchestratorRuntimeArnPattern = `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:runtime/${prefix.replace(/-/g, '_')}_orchestrator*`;
    this.uiTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'InvokeOrchestrator',
        actions: [
          'bedrock-agentcore:InvokeAgentRuntime',
          'bedrock-agentcore:InvokeAgentRuntimeForUser',
        ],
        resources: [orchestratorRuntimeArnPattern],
      }),
    );

    // OpenSearch 접근 정책: semantic-mcp role + 계정 principal(seed 스크립트용)만 허용.
    // 계정 root principal 은 "이 계정 안에서 IAM 권한을 가진 principal 에게 위임"을 의미하며
    // OpenSearch 도메인 접근 제어의 표준 패턴. seed 스크립트는 배포자 자격증명으로 인덱싱.
    (this.openSearchDomain as opensearch.Domain).addAccessPolicies(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        principals: [
          this.semanticMcpRole,
          new iam.AccountRootPrincipal(),
        ],
        actions: ['es:ESHttpGet', 'es:ESHttpPost', 'es:ESHttpPut', 'es:ESHttpHead'],
        resources: [`${this.openSearchDomain.domainArn}/*`],
      }),
    );

    // ───────────────────────── Outputs ─────────────────────────
    new CfnOutput(this, 'VpcId', { value: this.vpc.vpcId });
    new CfnOutput(this, 'AuroraClusterArn', {
      value: this.auroraCluster.clusterArn,
      description: 'Aurora cluster ARN (Data API resourceArn)',
      exportName: `${prefix}-aurora-cluster-arn`,
    });
    new CfnOutput(this, 'AuroraMasterSecretArn', {
      value: this.auroraCluster.secret?.secretArn ?? 'n/a',
      description: 'Aurora master credentials secret ARN (used by seed script)',
    });
    new CfnOutput(this, 'AgentRoSecretArn', {
      value: this.agentRoSecret.secretArn,
      description: 'Read-only DB user secret ARN (used by SQL MCP Data API calls)',
      exportName: `${prefix}-agent-ro-secret-arn`,
    });
    new CfnOutput(this, 'DbName', { value: config.dbName });
    new CfnOutput(this, 'OpenSearchEndpoint', {
      value: `https://${this.openSearchDomain.domainEndpoint}`,
      description: 'OpenSearch domain endpoint (https)',
      exportName: `${prefix}-opensearch-endpoint`,
    });
    new CfnOutput(this, 'OpenSearchIndex', { value: config.openSearchIndex });
    new CfnOutput(this, 'MemoryId', {
      value: this.memory.memoryId,
      description: 'AgentCore Memory ID',
      exportName: `${prefix}-memory-id`,
    });
    new CfnOutput(this, 'EcrOrchestratorUri', { value: this.ecrOrchestrator.repositoryUri });
    new CfnOutput(this, 'EcrSqlMcpUri', { value: this.ecrSqlMcp.repositoryUri });
    new CfnOutput(this, 'EcrSemanticMcpUri', { value: this.ecrSemanticMcp.repositoryUri });
    new CfnOutput(this, 'EcrUiUri', { value: this.ecrUi.repositoryUri });
    new CfnOutput(this, 'CognitoUserPoolId', { value: this.userPool.userPoolId });
    new CfnOutput(this, 'CognitoUserPoolClientId', { value: this.userPoolClient.userPoolClientId });
  }
}
