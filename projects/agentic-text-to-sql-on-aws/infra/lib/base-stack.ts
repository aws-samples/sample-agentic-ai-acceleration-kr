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
  aws_redshiftserverless as redshiftserverless,
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
  /** M4: datasource-admin-mcp 이미지 리포 */
  public readonly ecrAdminMcp: ecr.IRepository;
  /** M4: admin web(Next.js) 이미지 리포 */
  public readonly ecrAdminWeb: ecr.IRepository;

  public readonly orchestratorRole: iam.Role;
  public readonly sqlMcpRole: iam.Role;
  public readonly semanticMcpRole: iam.Role;
  public readonly uiTaskRole: iam.Role;
  /** M4: datasource-admin-mcp Runtime 실행 role */
  public readonly adminMcpRole: iam.Role;
  /** M4: admin web ECS task role (관리 평면 AWS SDK 직접 호출) */
  public readonly adminWebTaskRole: iam.Role;

  public readonly userPool: cognito.UserPool;
  public readonly userPoolClient: cognito.UserPoolClient;
  /** M3: E2E·orchestrator 용 M2M(USER_PASSWORD_AUTH) 테스트 클라이언트 */
  public readonly m2mClient: cognito.UserPoolClient;

  // ───────────── M3: Redshift Serverless (2번째 데이터 소스) ─────────────
  public readonly redshiftWorkgroup: redshiftserverless.CfnWorkgroup;
  public readonly redshiftRoSecret: secretsmanager.ISecret;
  /** Redshift workgroup ARN (sql-mcp Data API grant·env 주입) */
  public readonly redshiftWorkgroupArn: string;

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

    // ───────────────────────── Redshift Serverless (2번째 데이터 소스, M3) ─────────────────────────
    // ⚠️ Redshift Serverless workgroup 은 3개 AZ 에 걸친 서브넷을 요구한다(문서: serverless-usage-
    //    considerations / getting-started-cluster-in-vpc). base VPC 는 maxAzs:2 라 부족하고, 이미
    //    배포된 VPC 의 AZ 수를 늘리면 파괴적 교체가 발생하므로, Redshift 전용 3-AZ VPC 를 별도로 만든다.
    //    Data API(redshift-data)는 AWS API 평면이라 publiclyAccessible=false 여도 동작하므로
    //    NAT 없는 격리 서브넷으로 비용을 최소화한다(sql-mcp 는 Data API 로만 접근).
    const redshiftVpc = new ec2.Vpc(this, 'RedshiftVpc', {
      vpcName: `${prefix}-rs-vpc`,
      maxAzs: 3,
      natGateways: 0,
      subnetConfiguration: [
        { name: 'rs-isolated', subnetType: ec2.SubnetType.PRIVATE_ISOLATED, cidrMask: 24 },
      ],
    });

    // Redshift 전용 SG. Data API 만 사용하므로 인바운드 규칙은 두지 않는다(기본 deny).
    const redshiftSg = new ec2.SecurityGroup(this, 'RedshiftSg', {
      vpc: redshiftVpc,
      securityGroupName: `${prefix}-redshift-sg`,
      description: 'Redshift Serverless workgroup SG (Data API only; no direct inbound)',
      allowAllOutbound: true,
    });

    // read-only 사용자(agent_ro) 시크릿. seed 가 이 자격증명으로 Redshift 사용자를 만들고
    // SELECT-only 권한을 부여한다(READ-ONLY 4중 방어의 DB grant 계층 — Aurora 와 동형).
    this.redshiftRoSecret = new secretsmanager.Secret(this, 'RedshiftRoSecret', {
      secretName: config.redshiftRoSecretName,
      description: 'Read-only Redshift user credentials for the SQL execution MCP (Data API)',
      generateSecretString: {
        secretStringTemplate: JSON.stringify({ username: config.readOnlyDbUser }),
        generateStringKey: 'password',
        // Redshift 비밀번호 제약(ASCII, 인용부호·슬래시 등 일부 문자 불가) 회피용으로 구두점 제외.
        excludePunctuation: true,
        passwordLength: 32,
      },
    });

    // namespace: 관리자 자격증명은 Secrets Manager 자동 관리(manageAdminPassword) — 평문 노출 금지.
    const redshiftNamespace = new redshiftserverless.CfnNamespace(this, 'RedshiftNamespace', {
      namespaceName: config.redshiftNamespaceName,
      dbName: config.dbName,
      adminUsername: 'rs_admin',
      manageAdminPassword: true,
    });

    this.redshiftWorkgroup = new redshiftserverless.CfnWorkgroup(this, 'RedshiftWorkgroup', {
      workgroupName: config.redshiftWorkgroupName,
      namespaceName: config.redshiftNamespaceName,
      baseCapacity: config.redshiftBaseCapacity,
      publiclyAccessible: false,
      subnetIds: redshiftVpc.selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_ISOLATED })
        .subnetIds,
      securityGroupIds: [redshiftSg.securityGroupId],
    });
    // workgroup 은 namespace 생성 후에 만들어져야 한다(같은 namespaceName 참조).
    this.redshiftWorkgroup.addDependency(redshiftNamespace);

    // workgroup ARN 은 배포 시점에 workgroup id(UUID)가 붙어 결정적이지 않으므로 와일드카드로 표현.
    // sql-mcp role 에 대한 실제 권한 부여는 IAM role 정의 이후 섹션에서 수행한다.
    this.redshiftWorkgroupArn = `arn:${this.partition}:redshift-serverless:${this.region}:${this.account}:workgroup/*`;

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

    // ───────────────────────── ECR 리포 (M1 4개 + M4 2개) ─────────────────────────
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
    // M4: admin panel 2종(관리 도구 MCP + admin web)
    this.ecrAdminMcp = makeRepo('AdminMcp', config.ecrRepos.datasourceAdminMcp);
    this.ecrAdminWeb = makeRepo('AdminWeb', config.ecrRepos.adminWeb);

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

    // M3: E2E·orchestrator(서비스 위임) 용 M2M 클라이언트.
    // USER_PASSWORD_AUTH(ADMIN_NO_SRP 아님) 를 켜서 클라이언트 API 로 AccessToken 을 받는다.
    // Gateway 인바운드 JWT authorizer 의 allowedClients 에 이 client id 가 포함돼야 한다
    // (gateway-stack 이 base.m2mClient 를 참조). generateSecret false — public client.
    this.m2mClient = this.userPool.addClient('M2mClient', {
      userPoolClientName: `${prefix}-m2m-client`,
      generateSecret: false,
      authFlows: { userPassword: true, userSrp: false },
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

    // sql-mcp: Redshift Data API + GetCredentials + RS read-only 시크릿 read (M3, datasource="redshift").
    // ⚠️ workgroup id(UUID) 미상이라 redshift-data 액션은 계정/리전 스코프로 제한(base 의 runtime ARN
    //    패턴과 동일 사상). GetCredentials 는 workgroup/* 로 제한(계정 내 사실상 이 1개).
    this.sqlMcpRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'RedshiftDataApi',
        actions: [
          'redshift-data:ExecuteStatement',
          'redshift-data:DescribeStatement',
          'redshift-data:GetStatementResult',
          'redshift-data:CancelStatement',
        ],
        resources: ['*'],
      }),
    );
    this.sqlMcpRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'RedshiftServerlessGetCredentials',
        actions: ['redshift-serverless:GetCredentials'],
        resources: [this.redshiftWorkgroupArn],
      }),
    );
    this.redshiftRoSecret.grantRead(this.sqlMcpRole);

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

    // M3 gateway 모드: 서비스 계정(Cognito M2M) 비밀번호 시크릿 read + Gateway MCP 호출.
    // (cognito-idp initiate_auth 는 클라이언트 API 라 IAM 권한 불필요.)
    this.orchestratorRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'GatewayModeServiceAccountSecret',
        actions: ['secretsmanager:GetSecretValue'],
        resources: [
          `arn:${this.partition}:secretsmanager:${this.region}:${this.account}:secret:${prefix}/e2e/user-password-*`,
        ],
      }),
    );
    this.orchestratorRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'InvokeGateway',
        actions: ['bedrock-agentcore:InvokeGateway'],
        resources: [
          `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:gateway/${config.gatewayName}*`,
        ],
      }),
    );

    // ── M5: bundle 기반 프롬프트/모델 오버라이드(§9.5) ──
    // orchestrator 는 세션 시작 시 SSM 활성 bundle 포인터를 읽고(TTL 60s), 지정된 bundle
    // 버전의 components["orchestrator"] 에서 system_prompt/model_id 를 오버라이드한다.
    // ⚠️ 순환 회피: SSM 파라미터는 evaluation 스택 소유(base→evaluation 역참조는 사이클)이므로
    //    이름 규칙으로 ARN 을 조립한다. 파라미터명은 config 가 단일 원천.
    this.orchestratorRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ActiveBundlePointerRead',
        actions: ['ssm:GetParameter'],
        resources: [
          // `/agentic-t2sql/active-bundle` → ARN 리소스부는 선행 슬래시를 제거한 형태.
          `arn:${this.partition}:ssm:${this.region}:${this.account}:parameter${
            config.activeBundleParamName.startsWith('/')
              ? config.activeBundleParamName
              : `/${config.activeBundleParamName}`
          }`,
        ],
      }),
    );
    this.orchestratorRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ConfigurationBundleRead',
        actions: [
          'bedrock-agentcore:GetConfigurationBundle',
          'bedrock-agentcore:GetConfigurationBundleVersion',
        ],
        // bundle ID 는 서비스가 생성 시 무작위 접미사를 붙이며(`<name>-XXXXXXXXXX`),
        // bundle 자체를 CDK 가 만들지 않으므로(admin panel 최초 생성) 계정/리전 스코프의
        // configuration-bundle 와일드카드로 제한한다. 계정 내 bundle 은 이 데모의 것 뿐이다.
        resources: [
          `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:configuration-bundle/*`,
        ],
      }),
    );

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

    // ───────────────────────── M4: datasource-admin-mcp 실행 role ─────────────────────────
    // 관리 도구 평면(큐레이션·승인·데이터소스 등록/테스트·스키마 크롤). semantic 쓰기의 단일
    // 지점이므로 DynamoDB RW 를 갖는다.
    //
    // ⚠️ 순환 회피: semantic 테이블은 semantic 스택 소유이고 base→semantic 역참조는 사이클이므로,
    //    ARN 은 토큰이 아니라 리터럴 테이블명으로 구성한다(semantic-stack 의 semanticTableArnLiteral
    //    패턴과 동일 사상).
    const semanticTableArnLiteral = `arn:${this.partition}:dynamodb:${this.region}:${this.account}:table/${config.semanticTableName}`;
    this.adminMcpRole = new iam.Role(this, 'AdminMcpRole', {
      roleName: `${prefix}-admin-mcp-role`,
      assumedBy: agentCorePrincipal,
      // ⚠️ IAM role description 은 Latin-1 문자만 허용 — 한국어 불가(배포 실측).
      description: 'Execution role for the datasource-admin-mcp runtime (semantic curation / datasource admin)',
    });
    this.adminMcpRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'SemanticTableReadWrite',
        actions: [
          'dynamodb:GetItem',
          'dynamodb:Query',
          'dynamodb:Scan',
          'dynamodb:BatchGetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:BatchWriteItem',
        ],
        resources: [semanticTableArnLiteral, `${semanticTableArnLiteral}/index/*`],
      }),
    );
    // 엔티티 저장 시 임베딩 생성(published 문서가 OSIS 로 전파될 때 kNN 필드 필요).
    this.adminMcpRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'BedrockTitanEmbed',
        actions: ['bedrock:InvokeModel'],
        resources: [
          `arn:${this.partition}:bedrock:${this.region}::foundation-model/${config.embeddingModelId}`,
        ],
      }),
    );
    // 스키마 크롤(information_schema) — Aurora Data API + agent_ro 시크릿(read-only 사용자).
    this.adminMcpRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'AuroraDataApiForCrawl',
        actions: ['rds-data:ExecuteStatement', 'rds-data:BatchExecuteStatement'],
        resources: [this.auroraCluster.clusterArn],
      }),
    );
    this.agentRoSecret.grantRead(this.adminMcpRole);
    // Redshift 크롤 — sql-mcp 와 동일 사상(workgroup id 미상 → redshift-data 는 계정/리전 스코프).
    this.adminMcpRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'RedshiftDataApiForCrawl',
        actions: [
          'redshift-data:ExecuteStatement',
          'redshift-data:DescribeStatement',
          'redshift-data:GetStatementResult',
          'redshift-data:CancelStatement',
        ],
        resources: ['*'],
      }),
    );
    this.adminMcpRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'RedshiftServerlessGetCredentials',
        actions: ['redshift-serverless:GetCredentials'],
        resources: [this.redshiftWorkgroupArn],
      }),
    );
    this.redshiftRoSecret.grantRead(this.adminMcpRole);
    // 데이터소스 등록: 연결 자격증명을 `agentic-t2sql/datasource/<id>` 프리픽스에만 쓸 수 있다.
    this.adminMcpRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'DatasourceSecrets',
        actions: [
          'secretsmanager:CreateSecret',
          'secretsmanager:PutSecretValue',
          'secretsmanager:GetSecretValue',
          'secretsmanager:DescribeSecret',
          'secretsmanager:TagResource',
        ],
        resources: [
          `arn:${this.partition}:secretsmanager:${this.region}:${this.account}:secret:${prefix}/datasource/*`,
        ],
      }),
    );

    // ── M5 Track B: 후보 채굴기(mine_candidates) 의 orchestrator 로그 읽기 ──
    // orchestrator 가 남기는 `t2sql_query_record` JSON 을 CloudWatch 에서 수집한다.
    // ⚠️ logs:DescribeLogGroups 는 Runtime L2 가 이미 계정 내 log-group:* 로 자동 부여하므로
    //    (리소스 수준 권한 미지원 액션 — M4 admin-web 실측) 여기서는 중복 부여하지 않고
    //    실제 내용 읽기(FilterLogEvents)만 runtime 프리픽스로 제한해 추가한다.
    this.adminMcpRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'OrchestratorLogsRead',
        actions: ['logs:FilterLogEvents'],
        resources: [
          `arn:${this.partition}:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*`,
        ],
      }),
    );

    // ───────────────────────── M4: admin web ECS task role ─────────────────────────
    // admin panel 의 "관리 평면" 직접 호출용(도구 평면과 무관): Cognito 사용자·그룹 관리,
    // Cedar 정책 read-only 조회, CloudWatch·X-Ray 관측 조회. 큐레이션/데이터소스 작업은
    // 사용자 JWT → Gateway MCP 경로이므로 여기에 DynamoDB 권한을 두지 않는다(§8.0).
    this.adminWebTaskRole = new iam.Role(this, 'AdminWebTaskRole', {
      roleName: `${prefix}-admin-web-task-role`,
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      // ⚠️ IAM role description 은 Latin-1 문자만 허용 — 한국어 불가(배포 실측).
      description: 'Task role for the admin panel Fargate service (management-plane read / IAM admin)',
    });
    this.adminWebTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CognitoUserGroupAdmin',
        actions: [
          'cognito-idp:ListUsers',
          'cognito-idp:ListGroups',
          'cognito-idp:AdminGetUser',
          'cognito-idp:AdminCreateUser',
          'cognito-idp:AdminDeleteUser',
          'cognito-idp:AdminSetUserPassword',
          'cognito-idp:AdminAddUserToGroup',
          'cognito-idp:AdminRemoveUserFromGroup',
          'cognito-idp:AdminListGroupsForUser',
          'cognito-idp:ListUsersInGroup',
        ],
        // 이 user pool 한정(계정 내 다른 pool 접근 불가).
        resources: [this.userPool.userPoolArn],
      }),
    );
    // Cedar 정책 조회(read-only). policy-engine 은 gateway 스택 소유이므로 순환 회피를 위해
    // 이름 규칙 와일드카드 ARN 을 사용한다(gateway-stack 의 gateway ARN 패턴과 동일 사상).
    this.adminWebTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CedarPolicyReadOnly',
        // ListPolicyEngines(계정 단위 목록)은 부여하지 않는다 — admin web 은 env
        // POLICY_ENGINE_ID 로 대상 엔진을 알고 있어 목록 조회가 불필요하다.
        actions: [
          'bedrock-agentcore:GetPolicyEngine',
          'bedrock-agentcore:ListPolicies',
          'bedrock-agentcore:GetPolicy',
        ],
        resources: [
          `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:policy-engine/${config.policyEngineName}*`,
          `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:policy-engine/${config.policyEngineName}*/*`,
        ],
      }),
    );
    // 관측: CloudWatch 메트릭·로그·X-Ray 트레이스 조회. 메트릭/트레이스 API 는 리소스 수준
    // 권한을 지원하지 않아 '*' 가 유일한 표현(read-only 액션만 부여).
    this.adminWebTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ObservabilityRead',
        actions: [
          'cloudwatch:GetMetricData',
          'cloudwatch:GetMetricStatistics',
          'cloudwatch:ListMetrics',
          'xray:GetTraceSummaries',
          'xray:BatchGetTraces',
          'xray:GetServiceGraph',
          'xray:GetTraceGraph',
        ],
        resources: ['*'],
      }),
    );
    // ⚠️ logs:DescribeLogGroups 는 계정 수준 액션이라 특정 로그 그룹 프리픽스 ARN 으로
    //    스코프할 수 없다(요청 리소스가 `log-group::log-stream:` 로 평가됨 — M4 운영 실측,
    //    AccessDenied). 목록 조회만 계정 내 log-group:* 로 허용하고, 실제 내용 읽기
    //    (스트림·이벤트)는 runtime 프리픽스로 계속 제한한다.
    this.adminWebTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'LogGroupsList',
        actions: ['logs:DescribeLogGroups'],
        resources: [
          `arn:${this.partition}:logs:${this.region}:${this.account}:log-group:*`,
        ],
      }),
    );
    this.adminWebTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'RuntimeLogsRead',
        actions: [
          'logs:DescribeLogStreams',
          'logs:FilterLogEvents',
          'logs:GetLogEvents',
        ],
        resources: [
          `arn:${this.partition}:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*`,
        ],
      }),
    );
    // M5: StartBatchEvaluation 은 **호출자 자격증명**으로 트레이스 로그를 Insights 질의한다
    // (배포 실측: query 권한 없으면 "The evaluation execution role is missing required
    // CloudWatch Logs query permissions" ValidationException — 별도 role 파라미터 없음).
    // runtime 로그 그룹 + CloudWatch 스팬 저장소(aws/spans)로 스코프해 부여한다.
    this.adminWebTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'EvaluationLogsQuery',
        actions: ['logs:StartQuery', 'logs:GetQueryResults'],
        resources: [
          `arn:${this.partition}:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*`,
          `arn:${this.partition}:logs:${this.region}:${this.account}:log-group:aws/spans`,
          `arn:${this.partition}:logs:${this.region}:${this.account}:log-group:aws/spans:*`,
        ],
      }),
    );
    // M5: 배치 평가 결과는 서비스가 **호출자(FAS) 자격증명**으로 평가 결과 로그 그룹을
    // 만들어 기록한다(배포 실측: "FAS credentials do not have permission to create
    // CloudWatch log groups" → 이어서 "... to set log group retention policy").
    // evaluations 프리픽스로 스코프해 생성·쓰기·보존정책 액션을 부여한다.
    this.adminWebTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'EvaluationResultsLogWrite',
        actions: [
          'logs:CreateLogGroup',
          'logs:CreateLogStream',
          'logs:PutLogEvents',
          'logs:PutRetentionPolicy',
        ],
        resources: [
          `arn:${this.partition}:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/evaluations/*`,
        ],
      }),
    );

    // ───────────────────────── M5: admin web 평가·bundle 관리 권한 (§9.6) ─────────────────────────
    // Track A 화면(평가 실행·결과 조회·추천·bundle 승격)이 관리 평면 API 를 직접 호출한다.
    //
    // ⚠️ 리소스 스코프 주의: evaluation 스택이 만드는 evaluator/online-eval-config 는
    //    base→evaluation 역참조가 사이클이라 이름 규칙 와일드카드로 제한한다. batch-evaluation·
    //    recommendation·configuration-bundle 은 서비스가 무작위 접미사(`<name>-XXXXXXXXXX`)를
    //    붙여 생성하므로 요청 시점에 ARN 을 알 수 없다 → 계정/리전 스코프 타입별 와일드카드.
    // (a) 개별 리소스를 지목하는 액션 — 타입별 계정/리전 스코프 와일드카드로 제한.
    this.adminWebTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'EvaluationResourceScopedActions',
        actions: [
          'bedrock-agentcore:GetEvaluator',
          'bedrock-agentcore:GetOnlineEvaluationConfig',
          'bedrock-agentcore:GetBatchEvaluation',
          'bedrock-agentcore:StopBatchEvaluation',
          'bedrock-agentcore:GetRecommendation',
          'bedrock-agentcore:GetConfigurationBundle',
          'bedrock-agentcore:GetConfigurationBundleVersion',
          'bedrock-agentcore:ListConfigurationBundleVersions',
          'bedrock-agentcore:UpdateConfigurationBundle',
        ],
        // Delete* 는 부여하지 않는다(불변 버전 이력 보존 — 롤백은 SSM 포인터 되돌리기).
        resources: [
          `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:evaluator/*`,
          `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:online-evaluation-config/*`,
          `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:batch-evaluation/*`,
          `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:recommendation/*`,
          `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:configuration-bundle/*`,
        ],
      }),
    );
    // (b) 계정 단위 목록 액션 + 생성/시작 액션 — 대상 리소스가 요청 시점에 존재하지 않거나
    //     리소스 수준 권한을 지원하지 않아 '*' 가 유일한 표현이다(read/start 만, delete 없음).
    this.adminWebTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'EvaluationAccountScopedActions',
        actions: [
          'bedrock-agentcore:ListEvaluators',
          'bedrock-agentcore:ListOnlineEvaluationConfigs',
          'bedrock-agentcore:ListBatchEvaluations',
          'bedrock-agentcore:ListRecommendations',
          'bedrock-agentcore:ListConfigurationBundles',
          'bedrock-agentcore:StartBatchEvaluation',
          'bedrock-agentcore:StartRecommendation',
          'bedrock-agentcore:CreateConfigurationBundle',
        ],
        resources: ['*'],
      }),
    );
    // bundle 승격/롤백 = SSM 활성 포인터 전환(§9.1). 이 파라미터 1개로만 제한한다.
    // ⚠️ 순환 회피: 파라미터는 evaluation 스택 소유 → 이름 규칙으로 ARN 조립.
    this.adminWebTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ActiveBundlePointerReadWrite',
        actions: ['ssm:GetParameter', 'ssm:PutParameter'],
        resources: [
          `arn:${this.partition}:ssm:${this.region}:${this.account}:parameter${
            config.activeBundleParamName.startsWith('/')
              ? config.activeBundleParamName
              : `/${config.activeBundleParamName}`
          }`,
        ],
      }),
    );
    // StartBatchEvaluation 은 평가 실행 role 을 서비스에 넘길 수 있어야 한다(설치 SDK 의
    // 입력에는 role 파라미터가 없어 서비스가 online eval config 의 role 을 재사용할 수도
    // 있으나, 요구될 경우를 대비해 그 role 1개로만 PassRole 을 허용한다 — 최소 권한).
    this.adminWebTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'PassEvaluationExecutionRole',
        actions: ['iam:PassRole'],
        resources: [
          `arn:${this.partition}:iam::${this.account}:role/${config.evalExecutionRoleName}`,
        ],
        conditions: {
          StringEquals: { 'iam:PassedToService': 'bedrock-agentcore.amazonaws.com' },
        },
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
    // ── M4: admin panel 이미지 리포 ──
    new CfnOutput(this, 'EcrAdminMcpUri', {
      value: this.ecrAdminMcp.repositoryUri,
      description: 'datasource-admin-mcp ECR 리포 URI',
    });
    new CfnOutput(this, 'EcrAdminWebUri', {
      value: this.ecrAdminWeb.repositoryUri,
      description: 'admin web(Next.js) ECR 리포 URI',
    });
    new CfnOutput(this, 'CognitoUserPoolId', { value: this.userPool.userPoolId });
    new CfnOutput(this, 'CognitoUserPoolClientId', { value: this.userPoolClient.userPoolClientId });
    new CfnOutput(this, 'CognitoM2mClientId', {
      value: this.m2mClient.userPoolClientId,
      description: 'M2M(USER_PASSWORD_AUTH) 테스트 클라이언트 ID (E2E·orchestrator gateway 모드)',
      exportName: `${prefix}-m2m-client-id`,
    });

    // ── M3: Redshift Serverless outputs ──
    new CfnOutput(this, 'RedshiftWorkgroupName', {
      value: config.redshiftWorkgroupName,
      description: 'Redshift Serverless workgroup 이름 (Redshift Data API workgroupName)',
      exportName: `${prefix}-redshift-workgroup`,
    });
    new CfnOutput(this, 'RedshiftRoSecretArn', {
      value: this.redshiftRoSecret.secretArn,
      description: 'Redshift read-only(agent_ro) 시크릿 ARN (sql-mcp Data API)',
      exportName: `${prefix}-redshift-secret-arn`,
    });
  }
}
