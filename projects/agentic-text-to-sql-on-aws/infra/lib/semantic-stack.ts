import {
  Stack,
  StackProps,
  RemovalPolicy,
  Duration,
  CfnOutput,
  aws_ec2 as ec2,
  aws_dynamodb as dynamodb,
  aws_iam as iam,
  aws_lambda as lambda,
  aws_logs as logs,
  aws_sqs as sqs,
  aws_opensearchservice as opensearch,
  aws_osis as osis,
  aws_lambda_event_sources as eventsources,
} from 'aws-cdk-lib';
import * as neptune from '@aws-cdk/aws-neptune-alpha';
import { Construct } from 'constructs';
import { AppConfig } from './config';

export interface SemanticStackProps extends StackProps {
  readonly config: AppConfig;
  // base 스택에서 전달받는 참조들 (base → semantic 단방향 의존)
  readonly vpc: ec2.IVpc;
  readonly openSearchDomain: opensearch.IDomain;
  /** semantic-retrieval-mcp 실행 role (base 정의). 여기서 Neptune/DynamoDB 권한을 부여. */
  readonly semanticMcpRole: iam.Role;
}

/**
 * AgenticT2SqlSemanticStack — M2 semantic layer 저장·동기화 배관.
 *
 * 포함:
 *  - DynamoDB(system-of-record, Streams NEW_AND_OLD_IMAGES)
 *  - Neptune Serverless(그래프: 용어·join path·엔티티 계층)
 *  - OSIS 파이프라인: DynamoDB Streams → OpenSearch (published/v0 문서만)
 *  - graph-sync Lambda: DynamoDB Streams → Neptune upsert (DLQ 포함)
 *  - semantic-mcp role 에 Neptune/DynamoDB 데이터 액세스 권한 부여
 *
 * 스택 배선: Base → **Semantic** → Runtime → UI (bin/agentic-t2sql.ts). base 만 참조하므로
 * base↔semantic 순환은 없다. runtime 스택이 semantic 의 Neptune endpoint/SG 를 참조한다.
 *
 * ⚠️ tool layer Lambda 금지 제약과 무관: 아래 graph-sync Lambda 는 도구가 아니라
 *    Streams consumer(데이터 동기화 배관)이다 (ARCHITECTURE.md §4.4).
 */
export class AgenticT2SqlSemanticStack extends Stack {
  public readonly semanticTable: dynamodb.Table;
  public readonly graphCluster: neptune.DatabaseCluster;
  public readonly graphSecurityGroup: ec2.SecurityGroup;
  /** Runtime env 로 주입할 Neptune Gremlin/openCypher HTTPS 엔드포인트 */
  public readonly graphEndpoint: string;

  constructor(scope: Construct, id: string, props: SemanticStackProps) {
    super(scope, id, props);
    const { config } = props;
    const prefix = config.appPrefix;

    // ───────────────────────── DynamoDB (system-of-record) ─────────────────────────
    // pk/sk 복합키. sk="v0" 가 published 포인터, "v<N>" 가 버전 이력, "candidate" 가 미승인 초안.
    // Streams NEW_AND_OLD_IMAGES: OSIS(→OpenSearch)와 graph-sync Lambda(→Neptune) 두 소비자가 공유.
    // (DynamoDB Streams 는 샤드당 동시 소비자 2개까지 지원 — OSIS + Lambda = 2, 한도 내.)
    this.semanticTable = new dynamodb.Table(this, 'SemanticTable', {
      tableName: config.semanticTableName,
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
      // stream-only OSIS 구성이므로 PITR 불필요(초기 snapshot 없이 LATEST 스트림만 소비).
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // ───────────────────────── Neptune Serverless (그래프) ─────────────────────────
    // 전용 SG(인바운드는 아래에서 Lambda·runtime 로부터만 8182 허용), PRIVATE_ISOLATED 배치,
    // IAM 인증 활성(neptune-db:* data-access IAM 로 최소 권한 제어). RemovalPolicy.DESTROY(데모).
    this.graphSecurityGroup = new ec2.SecurityGroup(this, 'GraphSg', {
      vpc: props.vpc,
      securityGroupName: `${prefix}-graph-sg`,
      description: 'Neptune Serverless access - inbound 8182 from graph-sync Lambda and semantic MCP runtime',
      allowAllOutbound: true,
    });

    this.graphCluster = new neptune.DatabaseCluster(this, 'Graph', {
      dbClusterName: `${prefix}-graph`,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      securityGroups: [this.graphSecurityGroup],
      instanceType: neptune.InstanceType.SERVERLESS,
      instances: 1,
      serverlessScalingConfiguration: {
        minCapacity: config.graphMinNcu,
        maxCapacity: config.graphMaxNcu,
      },
      iamAuthentication: true,
      storageEncrypted: true,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // Neptune 데이터 액세스용 IAM 리소스 ARN. clusterResourceIdentifier(=cluster resource id)
    // 기반이며 클러스터 식별자(clusterIdentifier)와 다르다(문서: iam-data-resources).
    const graphDataArn = `arn:${this.partition}:neptune-db:${this.region}:${this.account}:${this.graphCluster.clusterResourceIdentifier}/*`;

    // Neptune 은 기본 8182 포트. socketAddress = "<hostname>:8182".
    this.graphEndpoint = `https://${this.graphCluster.clusterEndpoint.socketAddress}`;

    // ───────────────────────── graph-sync Lambda (Streams → Neptune) ─────────────────────────
    // DLQ: 배치 재시도 소진 시 실패 레코드 배치 메타데이터를 이 큐로 보낸다(onFailure).
    const graphSyncDlq = new sqs.Queue(this, 'GraphSyncDlq', {
      queueName: `${prefix}-semantic-sync-dlq`,
      retentionPeriod: Duration.days(14),
      enforceSSL: true,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const graphSyncFn = new lambda.Function(this, 'GraphSyncFn', {
      functionName: `${prefix}-graph-sync`,
      runtime: lambda.Runtime.PYTHON_3_13,
      // 다른 담당이 구현 중인 semantic-layer 패키지 참조. 코드 존재를 전제로 배선한다.
      code: lambda.Code.fromAsset('../semantic-layer/src'),
      handler: 'semantic_layer.lambda_handler.handler',
      timeout: Duration.seconds(60),
      memorySize: 256,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      environment: {
        // AWS_REGION 은 Lambda 예약 env 라 설정 금지.
        GRAPH_ENDPOINT: this.graphEndpoint,
        SEMANTIC_TABLE_NAME: this.semanticTable.tableName,
      },
      description: 'DynamoDB Streams consumer → Neptune openCypher upsert (semantic graph 동기화)',
    });

    // Neptune data-access: 최소 read/write/delete via query (neptune-db:* 대신 쿼리 액션만).
    graphSyncFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'NeptuneDataAccess',
        actions: [
          'neptune-db:connect',
          'neptune-db:ReadDataViaQuery',
          'neptune-db:WriteDataViaQuery',
          'neptune-db:DeleteDataViaQuery',
        ],
        resources: [graphDataArn],
      }),
    );
    // Lambda SG 로부터 Neptune 8182 인바운드 허용.
    this.graphCluster.connections.allowDefaultPortFrom(
      graphSyncFn,
      'graph-sync Lambda to Neptune 8182',
    );

    // Streams 이벤트 소스: 파샬 배치 응답 + DLQ. batchSize 10, TRIM_HORIZON, 재시도 3.
    // (DynamoEventSource 가 streams 읽기 권한을 실행 role 에 자동 부여.)
    graphSyncFn.addEventSource(
      new eventsources.DynamoEventSource(this.semanticTable, {
        startingPosition: lambda.StartingPosition.TRIM_HORIZON,
        batchSize: 10,
        retryAttempts: 3,
        bisectBatchOnError: true,
        reportBatchItemFailures: true,
        onFailure: new eventsources.SqsDlq(graphSyncDlq),
      }),
    );

    // ───────────────────────── OSIS 파이프라인 (DynamoDB → OpenSearch) ─────────────────────────
    // published(v0) 문서만 인덱싱. candidate/이력 버전은 drop_events 로 필터.
    //
    // ⚠️ 알려진 한계: drop_events 로 걸러지는 레코드는 OpenSearch 로 전파되지 않으므로,
    //    published → candidate 전환(unpublish)이나 v0 삭제가 인덱스에 반영되지 않아
    //    stale 문서가 남을 수 있다. Track B 승인 흐름은 publish 방향이 지배적이라 단순화 채택.
    //    필요 시 원본 DynamoDB 에서 인덱스 전체 재구축(backfill)으로 정합화한다.
    const pipelineLogGroup = new logs.LogGroup(this, 'OsisLogGroup', {
      // OSIS 로그 그룹명은 /aws/vendedlogs/ 접두어 필수.
      logGroupName: `/aws/vendedlogs/OpenSearchService/pipelines/${prefix}-semantic-sync`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // OSIS 파이프라인 실행 role. source(DynamoDB Streams 읽기) + sink(OpenSearch ESHttp*) 권한.
    const pipelineRole = new iam.Role(this, 'OsisPipelineRole', {
      roleName: `${prefix}-osis-pipeline-role`,
      assumedBy: new iam.ServicePrincipal('osis-pipelines.amazonaws.com'),
      description: 'OpenSearch Ingestion pipeline role: DynamoDB Streams to OpenSearch',
    });
    pipelineRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ReadDynamoStream',
        actions: [
          'dynamodb:DescribeTable',
          'dynamodb:DescribeStream',
          'dynamodb:GetRecords',
          'dynamodb:GetShardIterator',
        ],
        resources: [this.semanticTable.tableArn, `${this.semanticTable.tableArn}/stream/*`],
      }),
    );
    pipelineRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'WriteToOpenSearch',
        actions: [
          'es:DescribeDomain',
          'es:ESHttpGet',
          'es:ESHttpHead',
          'es:ESHttpPost',
          'es:ESHttpPut',
        ],
        resources: [
          props.openSearchDomain.domainArn,
          `${props.openSearchDomain.domainArn}/*`,
        ],
      }),
    );
    // OpenSearch 도메인 access policy 는 base 스택에서 이미 AccountRootPrincipal 을 허용하므로
    // (계정 내 IAM 권한 보유 principal 위임 패턴), 위 identity 권한만으로 파이프라인이 인덱싱할 수
    // 있다. base 스택의 addAccessPolicies 를 semantic 에서 다시 호출하면 base→semantic 순환이
    // 발생하므로 호출하지 않는다(도메인 FGAC 미사용 — IAM 접근 제어 경로가 유효).

    const region = this.region;
    const pipelineBody = [
      'version: "2"',
      'semantic-cdc-pipeline:',
      '  source:',
      '    dynamodb:',
      '      tables:',
      `        - table_arn: "${this.semanticTable.tableArn}"`,
      '          stream:',
      '            start_position: "LATEST"',
      '      aws:',
      `        region: "${region}"`,
      `        sts_role_arn: "${pipelineRole.roleArn}"`,
      '  processor:',
      '    - drop_events:',
      // published(v0) 만 통과: sk!=v0 이거나 status!=published 이면 드롭.
      '        drop_when: \'/sk != "v0" or /status != "published"\'',
      '  sink:',
      '    - opensearch:',
      `        hosts: ["https://${props.openSearchDomain.domainEndpoint}"]`,
      `        index: "${config.semanticIndex}"`,
      '        index_type: custom',
      '        document_id: "${getMetadata(\\"primary_key\\")}"',
      '        action: "${getMetadata(\\"opensearch_action\\")}"',
      '        document_version: "${getMetadata(\\"document_version\\")}"',
      '        document_version_type: "external"',
      '        aws:',
      `          region: "${region}"`,
      `          sts_role_arn: "${pipelineRole.roleArn}"`,
      '          serverless: false',
    ].join('\n');

    // 파이프라인명 27자(제약 3-28, 소문자·하이픈) — agentic-t2sql-semantic-sync.
    const pipeline = new osis.CfnPipeline(this, 'SemanticSyncPipeline', {
      pipelineName: `${prefix}-semantic-sync`,
      minUnits: 1,
      maxUnits: 1,
      pipelineConfigurationBody: pipelineBody,
      logPublishingOptions: {
        isLoggingEnabled: true,
        cloudWatchLogDestination: { logGroup: pipelineLogGroup.logGroupName },
      },
    });
    // 파이프라인 생성 전에 role/도메인/테이블/로그그룹이 준비돼야 한다.
    pipeline.node.addDependency(pipelineRole);
    pipeline.node.addDependency(pipelineLogGroup);
    pipeline.node.addDependency(this.semanticTable);

    // ───────────────────────── semantic-mcp role 권한 부여 (base role 확장) ─────────────────────────
    // base-stack 은 role 만 정의. Neptune 순회 + DynamoDB read 권한을 여기서 부여한다.
    //
    // ⚠️ 순환 의존 회피: semanticMcpRole 은 base 스택 리소스다. semantic 은 base(VPC)에 의존하므로,
    //    이 role 에 semantic 구축 토큰(Neptune clusterResourceId, table ARN 토큰)을 넣으면
    //    base→semantic 역방향 참조가 생겨 사이클이 된다. 따라서 base role 에 부여하는 ARN 은
    //    토큰이 아닌 결정적 값(계정/리전 pseudo + 리터럴 테이블명, cluster resource id 는 와일드카드)
    //    으로 구성한다(base-stack 의 runtime ARN 와일드카드 패턴과 동일 사상). 최소 권한 소폭 완화.
    const graphDataArnWildcard = `arn:${this.partition}:neptune-db:${this.region}:${this.account}:*/*`;
    const semanticTableArnLiteral = `arn:${this.partition}:dynamodb:${this.region}:${this.account}:table/${config.semanticTableName}`;
    props.semanticMcpRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'NeptuneReadForRetrieval',
        actions: ['neptune-db:connect', 'neptune-db:ReadDataViaQuery'],
        resources: [graphDataArnWildcard],
      }),
    );
    props.semanticMcpRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'SemanticTableRead',
        actions: ['dynamodb:GetItem', 'dynamodb:Query', 'dynamodb:BatchGetItem'],
        resources: [semanticTableArnLiteral, `${semanticTableArnLiteral}/index/*`],
      }),
    );
    // semantic MCP runtime SG → Neptune 8182 인바운드는 runtime-stack 에서 열어준다
    // (runtime→semantic 의존만 있으므로 ingress 규칙을 runtime 스택에 배치해야 사이클이 안 생긴다).

    // ───────────────────────── Outputs (exportName 계약 준수) ─────────────────────────
    new CfnOutput(this, 'SemanticTableName', {
      value: this.semanticTable.tableName,
      description: 'DynamoDB semantic system-of-record 테이블명',
      exportName: `${prefix}-semantic-table-name`,
    });
    new CfnOutput(this, 'SemanticTableArn', {
      value: this.semanticTable.tableArn,
      description: 'DynamoDB semantic 테이블 ARN',
      exportName: `${prefix}-semantic-table-arn`,
    });
    new CfnOutput(this, 'GraphEndpoint', {
      value: this.graphEndpoint,
      description: 'Neptune HTTPS 엔드포인트 (openCypher/Gremlin, 8182)',
      exportName: `${prefix}-graph-endpoint`,
    });
    new CfnOutput(this, 'GraphClusterResourceId', {
      value: this.graphCluster.clusterResourceIdentifier,
      description: 'Neptune 클러스터 리소스 ID (neptune-db IAM ARN 구성용)',
    });
    new CfnOutput(this, 'GraphSyncDlqUrl', {
      value: graphSyncDlq.queueUrl,
      description: 'graph-sync Lambda DLQ URL',
    });
    new CfnOutput(this, 'SemanticIndex', {
      value: config.semanticIndex,
      description: 'semantic 문서용 OpenSearch 인덱스명 (OSIS 싱크 대상)',
    });
    new CfnOutput(this, 'OsisPipelineName', {
      value: pipeline.pipelineName,
      description: 'OSIS DynamoDB→OpenSearch 파이프라인명',
    });
  }
}
