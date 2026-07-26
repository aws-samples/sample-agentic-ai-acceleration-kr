#!/usr/bin/env node
import { App, Environment } from 'aws-cdk-lib';
import { loadConfig } from '../lib/config';
import { AgenticT2SqlBaseStack } from '../lib/base-stack';
import { AgenticT2SqlSemanticStack } from '../lib/semantic-stack';
import { AgenticT2SqlRuntimeStack } from '../lib/runtime-stack';
import { AgenticT2SqlGatewayStack } from '../lib/gateway-stack';
import { AgenticT2SqlUiStack } from '../lib/ui-stack';
import { AgenticT2SqlAdminStack } from '../lib/admin-stack';

const app = new App();
const config = loadConfig(app);

// 계정은 하드코딩하지 않고 CDK_DEFAULT_ACCOUNT(배포자 환경)에서 읽는다.
// 리전은 cdk.json context 로 us-west-2 고정.
const env: Environment = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: config.region,
};

// 1) 기반 스택: VPC / Aurora / OpenSearch / ECR / Cognito / Memory / IAM roles
const base = new AgenticT2SqlBaseStack(app, 'AgenticT2SqlBaseStack', {
  env,
  config,
  description: 'Agentic Text-to-SQL — base infra (VPC, Aurora, OpenSearch, ECR, Cognito, Memory, IAM)',
});

// 2) semantic 스택: DynamoDB / Neptune / OSIS 동기화 / graph-sync Lambda (base 만 의존)
const semantic = new AgenticT2SqlSemanticStack(app, 'AgenticT2SqlSemanticStack', {
  env,
  config,
  vpc: base.vpc,
  openSearchDomain: base.openSearchDomain,
  semanticMcpRole: base.semanticMcpRole,
  description: 'Agentic Text-to-SQL — semantic layer (DynamoDB, Neptune, OSIS sync, graph-sync Lambda)',
});
semantic.addStackDependency(base);

// 3) 런타임 스택: AgentCore Runtime 3개 (이미지 push 후 배포). semantic 의 Neptune 참조.
const runtime = new AgenticT2SqlRuntimeStack(app, 'AgenticT2SqlRuntimeStack', {
  env,
  config,
  auroraCluster: base.auroraCluster,
  agentRoSecret: base.agentRoSecret,
  redshiftRoSecret: base.redshiftRoSecret,
  openSearchDomain: base.openSearchDomain,
  memory: base.memory,
  ecrOrchestrator: base.ecrOrchestrator,
  ecrSqlMcp: base.ecrSqlMcp,
  ecrSemanticMcp: base.ecrSemanticMcp,
  ecrAdminMcp: base.ecrAdminMcp,
  orchestratorRole: base.orchestratorRole,
  sqlMcpRole: base.sqlMcpRole,
  semanticMcpRole: base.semanticMcpRole,
  adminMcpRole: base.adminMcpRole,
  vpc: base.vpc,
  graphSecurityGroup: semantic.graphSecurityGroup,
  graphEndpoint: semantic.graphEndpoint,
  description: 'Agentic Text-to-SQL — AgentCore Runtimes (orchestrator + 3 MCP servers)',
});
runtime.addStackDependency(semantic);

// 3.5) Gateway 스택: Gateway·Cedar·Identity (M3). base(Cognito)·runtime(MCP ARN) 참조.
//      runtime 이후 배포(runtime→gateway 역참조 없음 → 사이클 없음). UI 와 무관.
const gateway = new AgenticT2SqlGatewayStack(app, 'AgenticT2SqlGatewayStack', {
  env,
  config,
  userPool: base.userPool,
  userPoolClient: base.userPoolClient,
  m2mClient: base.m2mClient,
  sqlMcpRole: base.sqlMcpRole,
  sqlMcpRuntime: runtime.sqlMcpRuntime,
  semanticMcpRuntime: runtime.semanticMcpRuntime,
  adminMcpRuntime: runtime.adminMcpRuntime,
  description: 'Agentic Text-to-SQL — Gateway·Cedar·Identity (M3 tool plane + M4 admin target)',
});
gateway.addStackDependency(runtime);

// 4) UI 스택: ECS Fargate + ALB (orchestrator runtime 호출)
const ui = new AgenticT2SqlUiStack(app, 'AgenticT2SqlUiStack', {
  env,
  config,
  vpc: base.vpc,
  ecrUi: base.ecrUi,
  uiTaskRole: base.uiTaskRole,
  orchestratorRuntime: runtime.orchestratorRuntime,
  description: 'Agentic Text-to-SQL — UI (ECS Fargate + public ALB)',
});
ui.addStackDependency(runtime);

// 5) Admin 스택: admin panel(Next.js) ECS Fargate + 전용 ALB (M4).
//    base(VPC·ECR·Cognito·task role) + gateway(gatewayUrl·policyEngineId) 참조 →
//    gateway 이후 배포(admin→gateway 단방향, 역참조 없음 → 사이클 없음).
const admin = new AgenticT2SqlAdminStack(app, 'AgenticT2SqlAdminStack', {
  env,
  config,
  vpc: base.vpc,
  ecrAdminWeb: base.ecrAdminWeb,
  adminWebTaskRole: base.adminWebTaskRole,
  userPool: base.userPool,
  m2mClient: base.m2mClient,
  gateway: gateway.gateway,
  policyEngine: gateway.policyEngine,
  description: 'Agentic Text-to-SQL — Admin panel (ECS Fargate + public ALB, M4)',
});
admin.addStackDependency(gateway);

app.synth();
