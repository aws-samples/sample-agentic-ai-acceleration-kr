import {
  Stack,
  StackProps,
  CfnOutput,
  Duration,
  aws_ec2 as ec2,
  aws_ecr as ecr,
  aws_ecs as ecs,
  aws_iam as iam,
  aws_logs as logs,
  aws_elasticloadbalancingv2 as elbv2,
  aws_cognito as cognito,
  aws_bedrockagentcore as agentcore,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { AppConfig } from './config';

export interface AdminStackProps extends StackProps {
  readonly config: AppConfig;
  // base 스택 참조
  readonly vpc: ec2.IVpc;
  readonly ecrAdminWeb: ecr.IRepository;
  readonly adminWebTaskRole: iam.Role;
  readonly userPool: cognito.IUserPool;
  /** USER_PASSWORD_AUTH 를 지원하는 M2M 클라이언트 — admin panel 로그인/토큰 발급에 사용 */
  readonly m2mClient: cognito.IUserPoolClient;
  // gateway 스택 참조 (도구 평면 OBO 호출 + Cedar 정책 조회)
  readonly gateway: agentcore.Gateway;
  readonly policyEngine: agentcore.CfnPolicyEngine;
  // ── M5: evaluation 스택 참조 (평가·bundle 관리 화면) ──
  // admin 은 evaluation 이후 배포되므로 객체 참조가 안전하다(admin→evaluation 단방향).
  readonly executionEvaluator: agentcore.Evaluator;
  readonly onlineEvalConfig: agentcore.OnlineEvaluationConfig;
  readonly activeBundleParamName: string;
  /** orchestrator 트레이스 로그 그룹명 (batch evaluation dataSourceConfig) */
  readonly orchestratorLogGroupName: string;
  /** orchestrator 관측 서비스명 `<runtime_name>.<endpoint>` */
  readonly orchestratorServiceName: string;
  /** online eval 실행 role ARN (batch evaluation 이 role 을 요구할 경우 사용) */
  readonly evalExecutionRoleArn: string;
}

/**
 * AgenticT2SqlAdminStack — M4 admin panel(Next.js) 을 ECS Fargate + 전용 퍼블릭 ALB 로 호스팅.
 *
 * ui-stack.ts 와 동형 구조이며 차이는 다음과 같다:
 *  - 컨테이너는 web + API routes 를 함께 서빙한다. API route 가 Cognito JWT(aws-jwt-verify)를
 *    검증하고 `cognito:groups` 로 Admin/Manager 기능을 분리한다.
 *  - 큐레이션·승인·데이터소스 작업은 **사용자 JWT Bearer → Gateway MCP → datasource-admin-mcp**
 *    경로다(§8.0). 즉 DynamoDB 직접 쓰기 없음 → semantic 쓰기 단일 지점 유지 + Cedar 인가 강제.
 *  - Cognito 사용자·그룹 관리 / Cedar 조회 / CloudWatch·X-Ray 조회는 task role 로 AWS SDK
 *    직접 호출(관리 평면 — 도구 평면과 무관).
 *  - 헬스체크는 `/api/health`(인증 불필요).
 *
 * 배포 순서: base → 이미지 push(admin-mcp) → runtime → gateway → gateway 2-phase
 *           (`-c cedarActionScoping=true`) → 이미지 push(admin-web) → **admin**.
 */
export class AgenticT2SqlAdminStack extends Stack {
  public readonly loadBalancerUrl: string;

  constructor(scope: Construct, id: string, props: AdminStackProps) {
    super(scope, id, props);
    const { config } = props;
    const prefix = config.appPrefix;

    const cluster = new ecs.Cluster(this, 'Cluster', {
      clusterName: `${prefix}-admin`,
      vpc: props.vpc,
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
    });

    // task role 은 base 소유(adminWebTaskRole — Cognito 관리·Cedar read·관측 read).
    // 여기서는 이미지 pull 용 execution role 만 만든다(ui-stack 패턴).
    const executionRole = new iam.Role(this, 'TaskExecutionRole', {
      roleName: `${prefix}-admin-web-exec-role`,
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          'service-role/AmazonECSTaskExecutionRolePolicy',
        ),
      ],
    });
    props.ecrAdminWeb.grantPull(executionRole);

    // admin panel 은 UI 대비 API 처리(MCP 호출·JWT 검증·집계)가 많아 UI(256/512)보다 여유를 둔다.
    const taskDef = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      family: `${prefix}-admin-web`,
      cpu: 512,
      memoryLimitMiB: 1024,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
      taskRole: props.adminWebTaskRole,
      executionRole,
    });

    taskDef.addContainer('web', {
      containerName: 'admin-web',
      image: ecs.ContainerImage.fromEcrRepository(props.ecrAdminWeb, 'latest'),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'admin-web',
        logRetention: logs.RetentionDays.ONE_WEEK,
      }),
      environment: {
        AWS_REGION: this.region,
        // JWT 검증(issuer/JWKS)·Cognito 관리 API 대상 pool
        COGNITO_USER_POOL_ID: props.userPool.userPoolId,
        // 로그인(USER_PASSWORD_AUTH)·audience 검증에 쓰는 클라이언트 — 웹 클라이언트는
        // userPassword 플로우가 비활성이라 m2m 클라이언트를 사용한다(§8.5).
        COGNITO_CLIENT_ID: props.m2mClient.userPoolClientId,
        // 사용자 JWT On-Behalf-Of 로 MCP 도구를 호출할 Gateway MCP 엔드포인트
        GATEWAY_URL: props.gateway.gatewayUrl ?? '',
        // 도구명 프리픽스(`<target>___<tool>`) 구성용 — gateway target 이름과 단일 원천 공유.
        ADMIN_MCP_TARGET: config.adminMcpTargetName,
        // Cedar 정책 read-only 조회 대상
        POLICY_ENGINE_ID: props.policyEngine.attrPolicyEngineId,
        // 세션 트레이스 탐색용 로그 그룹 접두어(task role 의 logs 리소스 제한과 일치)
        RUNTIME_LOG_GROUP_PREFIX: '/aws/bedrock-agentcore/runtimes/',
        // ───────── M5: 평가·bundle 관리 화면 (§9.7) ─────────
        // EX custom evaluator — 평가 실행 시 기본 선택 evaluator.
        EXECUTION_EVALUATOR_ID: props.executionEvaluator.evaluatorId,
        // online eval 상태 화면 + batch evaluation 의 onlineEvaluationConfigSource 소스.
        ONLINE_EVAL_CONFIG_ID: props.onlineEvalConfig.onlineEvaluationConfigId,
        // bundle 승격·롤백의 단일 원천(SSM). task role 이 이 파라미터만 Put 할 수 있다.
        ACTIVE_BUNDLE_PARAM: props.activeBundleParamName,
        // batch evaluation dataSourceConfig.cloudWatchLogs — 로그 그룹·서비스명은
        // evaluation 스택이 orchestrator Runtime 객체에서 유도한 값을 그대로 전달받는다.
        ORCHESTRATOR_LOG_GROUP: props.orchestratorLogGroupName,
        ORCHESTRATOR_SERVICE_NAME: props.orchestratorServiceName,
        // 평가 실행 role (StartBatchEvaluation 이 role 전달을 요구하는 경우 사용).
        EVAL_EXECUTION_ROLE_ARN: props.evalExecutionRoleArn,
      },
      portMappings: [{ containerPort: 3000, protocol: ecs.Protocol.TCP }],
    });

    const service = new ecs.FargateService(this, 'Service', {
      serviceName: `${prefix}-admin-web`,
      cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      assignPublicIp: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      minHealthyPercent: 100,
      // 이미지 회귀 시 자동 롤백(ui-stack 과 동일).
      circuitBreaker: { rollback: true },
    });

    const alb = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
      loadBalancerName: `${prefix}-admin-alb`,
      vpc: props.vpc,
      internetFacing: true,
    });
    const listener = alb.addListener('Http', {
      port: 80,
      protocol: elbv2.ApplicationProtocol.HTTP,
      open: true,
    });
    listener.addTargets('AdminTarget', {
      port: 3000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [service],
      healthCheck: {
        // 인증 불필요 경로(§8.4) — 루트는 로그인 리다이렉트 가능성이 있어 API health 를 쓴다.
        path: '/api/health',
        interval: Duration.seconds(30),
        healthyHttpCodes: '200-399',
      },
      deregistrationDelay: Duration.seconds(10),
    });

    this.loadBalancerUrl = `http://${alb.loadBalancerDnsName}`;
    new CfnOutput(this, 'AdminAlbUrl', {
      value: this.loadBalancerUrl,
      description: 'Public ALB URL for the admin panel (Admin/Manager 콘솔)',
      exportName: `${prefix}-admin-alb-url`,
    });
  }
}
