import {
  Stack,
  StackProps,
  CfnOutput,
  Duration,
  Arn,
  ArnFormat,
  aws_iam as iam,
  aws_lambda as lambda,
  aws_rds as rds,
  aws_secretsmanager as secretsmanager,
  aws_ssm as ssm,
  aws_bedrockagentcore as agentcore,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { AppConfig } from './config';

export interface EvaluationStackProps extends StackProps {
  readonly config: AppConfig;
  // base 스택 참조 (EX evaluator 가 gold SQL / 생성 SQL 을 read-only 로 재실행)
  readonly auroraCluster: rds.DatabaseCluster;
  readonly agentRoSecret: secretsmanager.ISecret;
  // runtime 스택 참조 (online eval 트레이스 소스 = orchestrator 런타임 로그)
  readonly orchestratorRuntime: agentcore.Runtime;
}

/**
 * AgenticT2SqlEvaluationStack — Track A(평가 파이프라인) 인프라.
 *
 * 구성:
 *  1) EX(Execution Accuracy) code-based evaluator Lambda (`agentic-t2sql-ex-evaluator`)
 *  2) AgentCore Evaluator (custom, code-based) — `agentic_t2sql_execution_accuracy`
 *  3) online eval 실행 role (`agentic-t2sql-eval-exec-role`)
 *  4) OnlineEvaluationConfig — `agentic_t2sql_online_eval`
 *     (builtin Correctness·ToolSelectionAccuracy + 위 custom evaluator, orchestrator 트레이스)
 *  5) SSM 활성 bundle 포인터 `/agentic-t2sql/active-bundle` (bundle 승격의 단일 원천)
 *
 * ⚠️ **CLAUDE.md 핵심 제약의 허용 예외**: "Tool layer 에 Lambda 금지" 규칙의 명시적 예외다.
 *    AgentCore Evaluations 의 code-based evaluator 는 서비스 규격상 **Lambda 함수만** 받는다
 *    (`EvaluatorConfig.codeBased({ lambdaFunction })` → CfnEvaluator 의 lambdaConfig).
 *    이 Lambda 는 "도구 평면"이 아니라 "평가 평면"이며, 에이전트가 호출하지 않는다
 *    (AgentCore Evaluations 서비스가 트레이스 단위로 호출).
 *
 * 배포 순서: base → semantic → runtime → gateway → gateway-scoped → **evaluation** → admin
 * (admin 이 이 스택의 출력을 컨테이너 env 로 소비하므로 admin 보다 먼저 배포된다.)
 */
export class AgenticT2SqlEvaluationStack extends Stack {
  /** EX evaluator Lambda (code-based evaluator 대상) */
  public readonly evaluatorFunction: lambda.Function;
  /** AgentCore custom evaluator (code-based) */
  public readonly executionEvaluator: agentcore.Evaluator;
  /** online eval 설정 */
  public readonly onlineEvalConfig: agentcore.OnlineEvaluationConfig;
  /** online eval 실행 role (admin panel 이 참고용 env 로 소비) */
  public readonly evalExecutionRole: iam.Role;
  /** SSM 활성 bundle 파라미터 */
  public readonly activeBundleParam: ssm.StringParameter;
  /** orchestrator 트레이스 로그 그룹명 (admin panel·batch eval 데이터 소스의 단일 원천) */
  public readonly orchestratorLogGroupName: string;
  /** orchestrator 관측 서비스명 `<runtime_name>.<endpoint>` (batch eval serviceNames) */
  public readonly orchestratorServiceName: string;

  constructor(scope: Construct, id: string, props: EvaluationStackProps) {
    super(scope, id, props);
    const { config } = props;
    const prefix = config.appPrefix;

    // ───────────────── 1) EX evaluator Lambda ─────────────────
    // 스팬에서 (질문, 생성 SQL) 을 추출 → goldset 매칭 → gold SQL 과 생성 SQL 을 각각
    // Aurora Data API(agent_ro 시크릿 = read-only 사용자)로 실행해 정규화 결과셋을 비교한다.
    // Data API 는 AWS API 평면이라 VPC 불필요(sql-execution-mcp 와 동일 사상).
    this.evaluatorFunction = new lambda.Function(this, 'ExEvaluatorFn', {
      functionName: config.exEvaluatorFunctionName,
      runtime: lambda.Runtime.PYTHON_3_13,
      // evaluation 패키지 참조(semantic-stack 의 graph-sync 와 동일 패턴). 런타임 의존성은
      // boto3 뿐이라 Lambda 기본 제공으로 충분 → 번들링 없이 소스 트리를 그대로 올린다.
      // goldset(`evaluation/src/evaluation/goldset/goldset-v1.jsonl`)도 이 asset 에 동봉된다.
      // __pycache__ 는 로컬 pytest 실행 여부에 따라 asset 해시가 흔들리므로 제외한다.
      code: lambda.Code.fromAsset('../evaluation/src', { exclude: ['**/__pycache__'] }),
      handler: 'evaluation.handler.handler',
      timeout: Duration.seconds(120),
      memorySize: 512,
      environment: {
        // AWS_REGION 은 Lambda 예약 env 라 설정 금지.
        AURORA_CLUSTER_ARN: props.auroraCluster.clusterArn,
        AURORA_SECRET_ARN: props.agentRoSecret.secretArn,
        DB_NAME: config.dbName,
        // 버저닝(§5.3): 평가 결과의 귀인을 위해 evaluator 로직 버전을 스탬프한다.
        EVALUATOR_VERSION: config.evaluatorVersion,
      },
      description:
        'Execution Accuracy(EX) code-based evaluator — gold SQL 대비 결과셋 동등성 비교 (read-only)',
    });

    // read-only 실행만 필요(SELECT). 트랜잭션 액션은 부여하지 않는다.
    this.evaluatorFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'AuroraDataApiReadOnly',
        actions: ['rds-data:ExecuteStatement'],
        resources: [props.auroraCluster.clusterArn],
      }),
    );
    props.agentRoSecret.grantRead(this.evaluatorFunction);

    // ───────────────── 2) AgentCore custom evaluator (code-based) ─────────────────
    // level=TRACE: 단일 요청-응답(질문 1건 → SQL 1건) 단위로 EX 를 계산한다.
    // L2 가 Lambda 에 resource-based permission 을 자동 부여한다(principal
    // bedrock-agentcore.amazonaws.com, sourceAccount=이 계정, sourceArn=이 evaluator ARN).
    // → 추가 grant 는 불필요(aws-cdk-lib 2.262 custom-evaluator.js 확인).
    this.executionEvaluator = new agentcore.Evaluator(this, 'ExecutionAccuracyEvaluator', {
      evaluatorName: config.executionEvaluatorName,
      level: agentcore.EvaluationLevel.TRACE,
      evaluatorConfig: agentcore.EvaluatorConfig.codeBased({
        lambdaFunction: this.evaluatorFunction,
        timeout: Duration.seconds(120),
      }),
      description: 'Execution Accuracy (EX): gold SQL 결과셋과 생성 SQL 결과셋의 동등성',
    });

    // ───────────────── 3) online eval 실행 role ─────────────────
    // L2 OnlineEvaluationConfig 는 executionRole 미지정 시 role 을 자동 생성하지만,
    // 이 이름(`agentic-t2sql-eval-exec-role`)을 config 계약으로 고정했고 admin panel 이
    // ARN 을 env 로 소비하므로 여기서 명시적으로 만들어 주입한다.
    // 권한 구성은 L2 의 createExecutionRole 과 동일 집합을 리소스 스코프로 재현했다
    // (aws-cdk-lib 2.262 online-evaluation.js / perms.js 기준).
    const dataSource = agentcore.DataSourceConfig.fromAgentRuntimeEndpoint(
      props.orchestratorRuntime,
    );
    this.orchestratorLogGroupName = dataSource.cloudWatchLogsConfig.logGroupNames[0];
    this.orchestratorServiceName = dataSource.cloudWatchLogsConfig.serviceNames[0];

    this.evalExecutionRole = new iam.Role(this, 'EvalExecutionRole', {
      roleName: config.evalExecutionRoleName,
      // 서비스가 evaluator / online-evaluation-config 리소스를 대신해 assume 한다
      // (confused deputy 방어: SourceAccount·ResourceAccount + SourceArn 조건).
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
        conditions: {
          StringEquals: {
            'aws:SourceAccount': this.account,
            'aws:ResourceAccount': this.account,
          },
          ArnLike: {
            'aws:SourceArn': [
              Arn.format({ service: 'bedrock-agentcore', resource: 'evaluator', resourceName: '*' }, this),
              Arn.format(
                { service: 'bedrock-agentcore', resource: 'online-evaluation-config', resourceName: '*' },
                this,
              ),
            ],
          },
        },
      }),
      // ⚠️ IAM role description 은 Latin-1 만 허용 — 한국어 불가(배포 실측).
      description: 'Execution role for Bedrock AgentCore online evaluation (agentic text-to-sql)',
    });

    // 트레이스를 읽어올 로그 그룹(orchestrator 런타임 로그 + CloudWatch 스팬 저장소 aws/spans).
    const logGroupArns = [this.orchestratorLogGroupName, 'aws/spans'].flatMap((name) => [
      Arn.format(
        { service: 'logs', resource: 'log-group', resourceName: name, arnFormat: ArnFormat.COLON_RESOURCE_NAME },
        this,
      ),
      Arn.format(
        { service: 'logs', resource: 'log-group', resourceName: `${name}:*`, arnFormat: ArnFormat.COLON_RESOURCE_NAME },
        this,
      ),
    ]);
    // DescribeLogGroups 는 리소스 수준 권한 미지원(계정 단위 목록 액션).
    this.evalExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CloudWatchLogDescribeStatement',
        actions: ['logs:DescribeLogGroups'],
        resources: ['*'],
      }),
    );
    this.evalExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CloudWatchLogQueryStatement',
        actions: ['logs:StartQuery', 'logs:GetQueryResults'],
        resources: logGroupArns,
      }),
    );
    // 평가 결과 기록 대상 로그 그룹(서비스가 생성).
    this.evalExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CloudWatchLogWriteStatement',
        actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [
          Arn.format(
            {
              service: 'logs',
              resource: 'log-group',
              resourceName: '/aws/bedrock-agentcore/evaluations/*',
              arnFormat: ArnFormat.COLON_RESOURCE_NAME,
            },
            this,
          ),
        ],
      }),
    );
    // 스팬 인덱스 정책(트레이스 조회 최적화) — aws/spans 한정.
    this.evalExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CloudWatchIndexPolicyStatement',
        actions: ['logs:DescribeIndexPolicies', 'logs:PutIndexPolicy'],
        resources: [
          Arn.format(
            { service: 'logs', resource: 'log-group', resourceName: 'aws/spans', arnFormat: ArnFormat.COLON_RESOURCE_NAME },
            this,
          ),
          Arn.format(
            { service: 'logs', resource: 'log-group', resourceName: 'aws/spans:*', arnFormat: ArnFormat.COLON_RESOURCE_NAME },
            this,
          ),
        ],
      }),
    );
    // builtin(LLM-as-a-judge) evaluator 는 Bedrock 모델을 호출한다. cross-region inference
    // profile 을 지원하려면 리전 와일드카드가 필요하다(L2 기본 동작과 동일).
    this.evalExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'BedrockInvokeStatement',
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [
          `arn:${this.partition}:bedrock:*::foundation-model/*`,
          `arn:${this.partition}:bedrock:*:${this.account}:inference-profile/*`,
        ],
      }),
    );
    // code-based evaluator Lambda 접근. CreateOnlineEvaluationConfig 가 생성 시점에 실행 role 의
    // `lambda:GetFunction` + `lambda:InvokeFunction` 보유를 검증한다(배포 실측 — grantInvoke 의
    // InvokeFunction 만으로는 ValidationException). 두 액션을 함수 1개로 스코프해 명시 부여한다.
    const lambdaAccess = this.evalExecutionRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'LambdaInvokeStatement',
        actions: ['lambda:InvokeFunction', 'lambda:GetFunction'],
        resources: [this.evaluatorFunction.functionArn, `${this.evaluatorFunction.functionArn}:*`],
      }),
    );

    // ───────────────── 4) OnlineEvaluationConfig ─────────────────
    // 데모이므로 샘플링 100%(운영에서는 비용에 맞춰 낮춘다).
    this.onlineEvalConfig = new agentcore.OnlineEvaluationConfig(this, 'OnlineEval', {
      onlineEvaluationConfigName: config.onlineEvalConfigName,
      executionRole: this.evalExecutionRole,
      evaluators: [
        agentcore.EvaluatorSelector.builtin(agentcore.BuiltinEvaluator.CORRECTNESS),
        agentcore.EvaluatorSelector.builtin(agentcore.BuiltinEvaluator.TOOL_SELECTION_ACCURACY),
        agentcore.EvaluatorSelector.custom(this.executionEvaluator),
      ],
      // orchestrator Runtime 의 DEFAULT 엔드포인트 트레이스(로그 그룹·서비스명 자동 유도).
      dataSource,
      samplingPercentage: config.onlineEvalSamplingPercentage,
      description: 'Orchestrator 트레이스 상시 평가 (Correctness · ToolSelectionAccuracy · EX)',
    });
    // ⚠️ 생성 시점 검증이 실행 role 권한을 확인하므로(위 실측), 정책이 role 에 적용된 뒤에
    //    config 가 생성되도록 명시적 의존을 건다(GatewayTarget policyDependable 과 동일 사상).
    if (lambdaAccess.policyDependable) {
      this.onlineEvalConfig.node.addDependency(lambdaAccess.policyDependable);
    }

    // ───────────────── 5) SSM 활성 bundle 포인터 ─────────────────
    // bundle 승격 = 이 파라미터 전환. 초기값은 빈 값 — orchestrator 는 빈 값/실패 시
    // 코드 기본값(system_prompt·model_id)으로 폴백한다(AGENTREL04).
    // ⚠️ 값은 admin panel 의 승격 API 가 갱신하므로, CFN 이 이후 배포에서 초기값으로
    //    되돌리지 않도록 이 파라미터를 손대지 않는다(값 변경은 런타임 소관).
    this.activeBundleParam = new ssm.StringParameter(this, 'ActiveBundleParam', {
      parameterName: config.activeBundleParamName,
      stringValue: JSON.stringify({ bundleId: '', versionId: '' }),
      description: '활성 Configuration Bundle 포인터 (승격·롤백의 단일 원천)',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ───────────────────────── Outputs (evaluation-outputs.json) ─────────────────────────
    new CfnOutput(this, 'ExecutionEvaluatorId', {
      value: this.executionEvaluator.evaluatorId,
      description: 'EX(Execution Accuracy) custom evaluator ID',
      exportName: `${prefix}-execution-evaluator-id`,
    });
    new CfnOutput(this, 'ExecutionEvaluatorArn', {
      value: this.executionEvaluator.evaluatorArn,
      description: 'EX(Execution Accuracy) custom evaluator ARN',
    });
    new CfnOutput(this, 'OnlineEvalConfigId', {
      value: this.onlineEvalConfig.onlineEvaluationConfigId,
      description: 'Online evaluation config ID (orchestrator 트레이스 상시 평가)',
      exportName: `${prefix}-online-eval-config-id`,
    });
    new CfnOutput(this, 'ActiveBundleParamName', {
      value: this.activeBundleParam.parameterName,
      description: '활성 bundle 포인터 SSM 파라미터명',
      exportName: `${prefix}-active-bundle-param`,
    });
    new CfnOutput(this, 'EvaluatorLambdaArn', {
      value: this.evaluatorFunction.functionArn,
      description: 'EX evaluator Lambda ARN (code-based evaluator 대상)',
    });
    new CfnOutput(this, 'EvalExecutionRoleArn', {
      value: this.evalExecutionRole.roleArn,
      description: 'Online evaluation 실행 role ARN',
    });
    new CfnOutput(this, 'OrchestratorLogGroup', {
      value: this.orchestratorLogGroupName,
      description: 'orchestrator 트레이스 로그 그룹 (batch eval·채굴 데이터 소스)',
    });
    new CfnOutput(this, 'OrchestratorServiceName', {
      value: this.orchestratorServiceName,
      description: 'orchestrator 관측 서비스명 (<runtime_name>.<endpoint>)',
    });
  }
}
