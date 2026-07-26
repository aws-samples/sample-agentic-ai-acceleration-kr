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
  aws_bedrockagentcore as agentcore,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { AppConfig } from './config';

export interface UiStackProps extends StackProps {
  readonly config: AppConfig;
  readonly vpc: ec2.IVpc;
  readonly ecrUi: ecr.IRepository;
  readonly uiTaskRole: iam.Role;
  readonly orchestratorRuntime: agentcore.Runtime;
}

/**
 * AgenticT2SqlUiStack — ECS Fargate + 퍼블릭 ALB 로 UI(Next.js + CopilotKit) 호스팅.
 *
 * task role 에 orchestrator runtime 호출 권한(대상 ARN 한정)만 부여.
 * desired count 1, 최소 사양(256 CPU / 512 MiB).
 */
export class AgenticT2SqlUiStack extends Stack {
  public readonly loadBalancerUrl: string;

  constructor(scope: Construct, id: string, props: UiStackProps) {
    super(scope, id, props);
    const { config } = props;
    const prefix = config.appPrefix;

    const cluster = new ecs.Cluster(this, 'Cluster', {
      clusterName: `${prefix}-ui`,
      vpc: props.vpc,
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
    });

    // uiTaskRole 의 orchestrator 호출 권한은 base 스택에서 결정적 ARN 규칙으로 부여됨
    // (순환 의존 방지). 여기서는 컨테이너 이미지 pull 용 execution role 만 구성한다.
    const executionRole = new iam.Role(this, 'TaskExecutionRole', {
      roleName: `${prefix}-ui-exec-role`,
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          'service-role/AmazonECSTaskExecutionRolePolicy',
        ),
      ],
    });
    props.ecrUi.grantPull(executionRole);

    const taskDef = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      family: `${prefix}-ui`,
      cpu: 256,
      memoryLimitMiB: 512,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
      taskRole: props.uiTaskRole,
      executionRole,
    });

    taskDef.addContainer('web', {
      containerName: 'ui',
      image: ecs.ContainerImage.fromEcrRepository(props.ecrUi, 'latest'),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'ui',
        logRetention: logs.RetentionDays.ONE_WEEK,
      }),
      environment: {
        AGENT_RUNTIME_ARN: props.orchestratorRuntime.agentRuntimeArn,
        AWS_REGION: this.region,
      },
      portMappings: [{ containerPort: 3000, protocol: ecs.Protocol.TCP }],
    });

    const service = new ecs.FargateService(this, 'Service', {
      serviceName: `${prefix}-ui`,
      cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      assignPublicIp: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      minHealthyPercent: 100,
      circuitBreaker: { rollback: true },
    });

    const alb = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
      loadBalancerName: `${prefix}-ui-alb`,
      vpc: props.vpc,
      internetFacing: true,
    });
    const listener = alb.addListener('Http', {
      port: 80,
      protocol: elbv2.ApplicationProtocol.HTTP,
      open: true,
    });
    listener.addTargets('UiTarget', {
      port: 3000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [service],
      healthCheck: {
        path: '/',
        interval: Duration.seconds(30),
        healthyHttpCodes: '200-399',
      },
      deregistrationDelay: Duration.seconds(10),
    });

    this.loadBalancerUrl = `http://${alb.loadBalancerDnsName}`;
    new CfnOutput(this, 'AlbUrl', {
      value: this.loadBalancerUrl,
      description: 'Public ALB URL for the UI web app',
      exportName: `${prefix}-ui-alb-url`,
    });
  }
}
