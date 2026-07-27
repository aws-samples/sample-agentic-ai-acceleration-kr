import {
  Stack,
  StackProps,
  CfnOutput,
  aws_iam as iam,
  aws_cognito as cognito,
  aws_bedrockagentcore as agentcore,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { AppConfig } from './config';

export interface GatewayStackProps extends StackProps {
  readonly config: AppConfig;
  // base 스택 참조 (인바운드 JWT authorizer 의 Cognito discovery·allowedClients)
  readonly userPool: cognito.IUserPool;
  readonly userPoolClient: cognito.IUserPoolClient;
  readonly m2mClient: cognito.IUserPoolClient;
  /** sql-mcp 실행 role (base 소유). Redshift 권한은 base 에서 이미 부여, 여기선 미사용이나 계약상 보관 */
  readonly sqlMcpRole: iam.IRole;
  // runtime 스택 참조 (MCP target 이 가리킬 두 Runtime ARN)
  readonly sqlMcpRuntime: agentcore.Runtime;
  readonly semanticMcpRuntime: agentcore.Runtime;
  /** datasource-admin-mcp runtime (3번째 MCP target) */
  readonly adminMcpRuntime: agentcore.Runtime;
}

/**
 * AgenticT2SqlGatewayStack — 도구 평면(tool plane).
 *
 * 포함:
 *  - Gateway `agentic-t2sql-gateway` (MCP protocol + semantic search 기본)
 *    · 인바운드: Cognito CustomJwt(웹 클라이언트 + M2M 테스트 클라이언트 allowed)
 *  - GatewayTarget 3개: sql-execution-mcp / semantic-retrieval-mcp / datasource-admin-mcp
 *    · McpServerTargetConfiguration → 각 Runtime 의 MCP invocations 엔드포인트
 *    · 아웃바운드 SigV4: GatewayCredentialProvider.fromIamRole(service "bedrock-agentcore")
 *    · Gateway 서비스 role 에 InvokeAgentRuntime(두 Runtime ARN 한정) 부여
 *  - PolicyEngine + Cedar 정책 3개 (Gateway 에 ENFORCE 로 연결)
 *
 * 배포 순서: Base → Semantic → Runtime → **Gateway** (UI 와 무관). runtime ARN 을 참조하므로
 *           runtime 이후에 배포된다(runtime→gateway 역참조 없음 → 사이클 없음).
 *
 * ⚠️ tool layer Lambda 금지 제약 준수: 도구는 Runtime 호스팅 MCP 서버이고, Gateway 는 그
 *    MCP 서버를 target 으로 집약하는 단일 도구 평면이다(ARCHITECTURE.md §4.3).
 */
export class AgenticT2SqlGatewayStack extends Stack {
  public readonly gateway: agentcore.Gateway;
  public readonly policyEngine: agentcore.CfnPolicyEngine;

  constructor(scope: Construct, id: string, props: GatewayStackProps) {
    super(scope, id, props);
    const { config } = props;
    const prefix = config.appPrefix;

    // ───────────────────────── 인바운드 인증: Cognito CustomJwt ─────────────────────────
    // Gateway 는 들어오는 JWT 를 Cognito user pool 의 OIDC discovery 로 검증한다.
    // discoveryUrl 형식은 고정: .../{userPoolId}/.well-known/openid-configuration.
    // allowedClients 에 웹 클라이언트 + M2M 테스트 클라이언트를 등록(둘 다 이 pool 발급 토큰).
    const discoveryUrl = `https://cognito-idp.${this.region}.amazonaws.com/${props.userPool.userPoolId}/.well-known/openid-configuration`;

    // ───────────────────────── Gateway ─────────────────────────
    // protocolConfiguration 기본값(MCP + SEMANTIC search)을 그대로 사용.
    this.gateway = new agentcore.Gateway(this, 'Gateway', {
      gatewayName: config.gatewayName,
      description: 'Agentic Text-to-SQL 단일 도구 평면 (MCP aggregation + semantic tool search)',
      authorizerConfiguration: agentcore.GatewayAuthorizer.usingCustomJwt({
        discoveryUrl,
        allowedClients: [
          props.userPoolClient.userPoolClientId,
          props.m2mClient.userPoolClientId,
        ],
      }),
    });

    // ───────────────────────── 아웃바운드 인증 (SigV4, MCP server target) ─────────────────────────
    // MCP server / OpenAPI target 만 service/region 명시가 허용된다. Runtime 호스팅 MCP 는
    // SigV4 서명 서비스명이 "bedrock-agentcore" (mcp_client.py 의 direct 모드와 정합).
    const iamCredential = agentcore.GatewayCredentialProvider.fromIamRole({
      service: 'bedrock-agentcore',
      region: this.region,
    });

    // Runtime → MCP invocations 엔드포인트 URL(percent-encoded ARN 경로).
    const sqlEndpoint = this.buildRuntimeMcpEndpoint(props.sqlMcpRuntime.agentRuntimeId);
    const semanticEndpoint = this.buildRuntimeMcpEndpoint(props.semanticMcpRuntime.agentRuntimeId);
    const adminEndpoint = this.buildRuntimeMcpEndpoint(props.adminMcpRuntime.agentRuntimeId);

    const sqlTarget = agentcore.GatewayTarget.forMcpServer(this, 'SqlMcpTarget', {
      gateway: this.gateway,
      gatewayTargetName: config.sqlTargetName,
      description: 'SQL 검증·실행 MCP (Aurora/Redshift Data API)',
      endpoint: sqlEndpoint,
      credentialProviderConfigurations: [iamCredential],
    });
    const semanticTarget = agentcore.GatewayTarget.forMcpServer(this, 'SemanticMcpTarget', {
      gateway: this.gateway,
      gatewayTargetName: config.semanticTargetName,
      description: '스키마/용어/동의어/few-shot 검색 MCP (OpenSearch hybrid + Neptune)',
      endpoint: semanticEndpoint,
      credentialProviderConfigurations: [iamCredential],
    });
    // 관리 도구 target. 도구명은 `datasource-admin-mcp___<tool>` 로 노출된다.
    // 일반 User 는 Cedar default-deny(phase 2 에서 action 스코프)로 이 도구군에 접근하지 못하고,
    // Manager/Admin 만 permitPrivileged 로 허용된다.
    const adminTarget = agentcore.GatewayTarget.forMcpServer(this, 'AdminMcpTarget', {
      gateway: this.gateway,
      gatewayTargetName: config.adminMcpTargetName,
      description: 'semantic 큐레이션·승인 + 데이터소스 등록/테스트/크롤 관리 도구 MCP (Manager/Admin 전용)',
      endpoint: adminEndpoint,
      credentialProviderConfigurations: [iamCredential],
    });
    // 타깃 간 결정적 생성 순서(선택). 참조로 사용해 lint 미사용 경고 방지.
    semanticTarget.node.addDependency(sqlTarget);
    adminTarget.node.addDependency(semanticTarget);

    // ───────────────────────── Gateway 서비스 role 에 InvokeAgentRuntime 부여 ─────────────────────────
    // fromIamRole 아웃바운드는 Gateway 의 실행 role 로 SigV4 서명한다. MCP server target 은
    // grantNeededPermissionsToRole 이 no-op 이므로, 실제 InvokeAgentRuntime 권한은 직접 부여한다.
    // 대상은 세 Runtime ARN 으로 한정(최소 권한). ARN 은 base/runtime 의 결정적 이름 규칙과 정합.
    //
    // ⚠️ target 생성 시 Gateway 서비스가 이 role 로 MCP 서버에 접속해 도구 목록을 fetch·검증한다
    //    (배포 실측: 정책 갱신 전에 target 이 만들어지면 "Authorization error when sending
    //    message" 로 NotStabilized). 따라서 모든 target 이 이 정책 갱신 이후에 생성되도록
    //    policyDependable 에 명시적 의존을 건다.
    const invokeGrant = this.gateway.role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'InvokeMcpRuntimes',
        actions: ['bedrock-agentcore:InvokeAgentRuntime'],
        resources: [
          props.sqlMcpRuntime.agentRuntimeArn,
          `${props.sqlMcpRuntime.agentRuntimeArn}/*`,
          props.semanticMcpRuntime.agentRuntimeArn,
          `${props.semanticMcpRuntime.agentRuntimeArn}/*`,
          // 관리 도구 runtime
          props.adminMcpRuntime.agentRuntimeArn,
          `${props.adminMcpRuntime.agentRuntimeArn}/*`,
        ],
      }),
    );
    if (invokeGrant.policyDependable) {
      for (const target of [sqlTarget, semanticTarget, adminTarget]) {
        target.node.addDependency(invokeGrant.policyDependable);
      }
    }

    // ───────────────────────── PolicyEngine + Cedar 정책 ─────────────────────────
    // 엔진은 default-deny + forbid-wins. 아래 정책 3개로 페르소나 기반 접근을 표현한다.
    // ⚠️ PolicyEngine/Policy 이름 패턴은 ^[A-Za-z][A-Za-z0-9_]*$ (하이픈 불가) — config 에서 언더스코어.
    this.policyEngine = new agentcore.CfnPolicyEngine(this, 'PolicyEngine', {
      name: config.policyEngineName,
      description: 'Agentic Text-to-SQL Cedar 정책 엔진 (페르소나 기반 도구 접근 제어)',
    });

    const gwArn = this.gateway.gatewayArn;

    // 정책 1 (permit): Manager 또는 Admin 그룹은 모든 도구 허용.
    //   cognito:groups 는 배열 claim → 문자열 tag 로 직렬화되므로 `like "*Manager*"` 로 매칭한다
    //   (AWS 공식 예제와 동일 패턴: principal.getTag("cognito:groups") like "*policyholders*").
    const permitPrivileged = `permit(
  principal is AgentCore::OAuthUser,
  action,
  resource == AgentCore::Gateway::"${gwArn}"
) when {
  principal.hasTag("cognito:groups") &&
  (principal.getTag("cognito:groups") like "*Manager*" ||
   principal.getTag("cognito:groups") like "*Admin*")
};`;

    // 정책 2 (permit): 인증된 사용자(일반 User)의 도구 허용 범위.
    //
    //   ⚠️ 배포 실측: action 목록(`action in [AgentCore::Action::"<Target>___<tool>"]`)으로 좁히는
    //   시도는 정책 생성 시점 검증에서 "unable to find an applicable action" 으로 실패한다
    //   (CFN 상 정책이 target 도구 동기화 완료 전에 검증됨). 그래서 2-phase 로 처리한다:
    //
    //     phase 1 (cedarActionScoping=false, 기본): 광역 permit — admin target 을 포함한 gateway
    //             배포가 먼저 성공하도록 한다. 이 시점엔 admin 도구도 일반 User 에게 열려 있다.
    //     phase 2 (cedarActionScoping=true): 도구 동기화가 끝난 뒤 statement 만 action 목록
    //             스코프로 교체(논리 ID 동일 → CFN update). 일반 User 는 run_sql/search_schema
    //             만 허용되고 datasource-admin-mcp___* 는 default-deny 로 차단된다
    //             (Manager/Admin 은 정책 1 의 광역 permit 으로 계속 전체 허용).
    //
    //   ⚠️ phase 2 는 admin target 배포 후에만 성공한다. `-c cedarActionScoping=true` 로
    //      gateway 를 재배포한다(scripts/deploy.sh gateway-scoped).
    const scopedActions = [
      `AgentCore::Action::"${config.sqlTargetName}___run_sql"`,
      `AgentCore::Action::"${config.semanticTargetName}___search_schema"`,
    ].join(', ');
    const permitBaseline = config.cedarActionScoping
      ? `permit(
  principal is AgentCore::OAuthUser,
  action in [${scopedActions}],
  resource == AgentCore::Gateway::"${gwArn}"
);`
      : `permit(
  principal is AgentCore::OAuthUser,
  action,
  resource == AgentCore::Gateway::"${gwArn}"
);`;

    // 정책 3 (forbid, 거부 검증용): "Denied" 그룹 principal 은 모든 action 차단.
    //   forbid 가 permit 을 이기므로(Cedar forbid-wins), E2E 가 Denied 사용자로 허용 목록 안의
    //   도구를 호출해도 deny 되는 것을 확인할 수 있다("인증됐지만 차단" 검증 경로).
    const forbidDenied = `forbid(
  principal is AgentCore::OAuthUser,
  action,
  resource == AgentCore::Gateway::"${gwArn}"
) when {
  principal.hasTag("cognito:groups") &&
  principal.getTag("cognito:groups") like "*Denied*"
};`;

    const mkPolicy = (idSuffix: string, name: string, statement: string, desc: string) =>
      new agentcore.CfnPolicy(this, `Policy${idSuffix}`, {
        name,
        policyEngineId: this.policyEngine.attrPolicyEngineId,
        definition: { cedar: { statement } },
        enforcementMode: 'ACTIVE',
        // Cedar analyzer 가 findings(예: always-allow)를 내도 배포 실패하지 않도록 무시.
        // (permitPrivileged/baseline 은 의도된 permit 이라 always-allow 경고가 정상)
        validationMode: 'IGNORE_ALL_FINDINGS',
        description: desc,
      });

    const p1 = mkPolicy(
      'PermitPrivileged',
      'permit_manager_admin_all_tools',
      permitPrivileged,
      'Manager/Admin 그룹: 모든 도구 허용',
    );
    // ⚠️ 논리 ID(`PolicyPermitBaseline`)·정책명은 두 phase 에서 동일하게 유지한다.
    //    statement 만 바뀌므로 CFN 이 교체가 아닌 update 로 처리한다.
    const p2 = mkPolicy(
      'PermitBaseline',
      'permit_authenticated_search_and_run',
      permitBaseline,
      config.cedarActionScoping
        ? '일반 인증 사용자: search_schema/run_sql 만 허용 (admin 도구는 default-deny)'
        : '일반 인증 사용자: 전체 허용 (phase 1 — action 스코프는 cedarActionScoping=true 재배포로 적용)',
    );
    const p3 = mkPolicy(
      'ForbidDenied',
      'forbid_denied_group_all',
      forbidDenied,
      '거부 검증용: Denied 그룹은 모든 action forbid (forbid-wins)',
    );
    // 정책들은 엔진 생성 이후에 만들어져야 한다.
    [p1, p2, p3].forEach((p) => p.addDependency(this.policyEngine));

    // ───────────────────────── Gateway ↔ PolicyEngine 연결 (escape hatch) ─────────────────────────
    // L2 Gateway 에는 policyEngine prop 이 없다(gateway.d.ts 확인). CfnGateway 의
    // policyEngineConfiguration 을 escape hatch 로 설정한다. mode 는 ENFORCE(=ACTIVE 강제).
    // Cedar statement 에 gateway ARN 토큰을 문자열 결합했으므로 배포 시 해석된다(2-phase 불필요).
    const cfnGw = this.gateway.node.defaultChild as agentcore.CfnGateway;
    cfnGw.policyEngineConfiguration = {
      arn: this.policyEngine.attrPolicyEngineArn,
      mode: 'ENFORCE',
    };

    // PolicyEngine 연동 권한: Gateway 생성 시 서비스가 gateway role 로 GetPolicyEngine 을
    // 호출해 연결을 검증한다(배포 실측 — 권한 없으면 CREATE_FAILED). 평가용 read 권한 포함.
    // Gateway 리소스 생성 전에 이 정책이 적용돼야 하므로 명시적 의존을 건다.
    const policyEnginePerms = new iam.Policy(this, 'GatewayPolicyEngineAccess', {
      roles: [this.gateway.role as iam.Role],
      statements: [
        new iam.PolicyStatement({
          sid: 'PolicyEngineRead',
          actions: [
            'bedrock-agentcore:GetPolicyEngine',
            'bedrock-agentcore:ListPolicies',
            'bedrock-agentcore:GetPolicy',
            'bedrock-agentcore:EvaluatePolicies',
            // 런타임 정책 평가(도구 호출 인가) — 배포 검증(GenesisPolicyEngineCheck)도 요구.
            // AuthorizeAction/PartiallyAuthorizeActions 는 policy-engine ARN 과 gateway ARN
            // 양쪽을 리소스로 검사한다. gateway ARN 은 생성 전이라 참조 불가(순환) →
            // 이름 규칙 와일드카드로 부여. 평가 계열 액션은 와일드카드로 묶는다(신규 액션 대응).
            'bedrock-agentcore:AuthorizeAction',
            'bedrock-agentcore:PartiallyAuthorizeActions',
            'bedrock-agentcore:BatchAuthorizeActions',
          ],
          resources: [
            this.policyEngine.attrPolicyEngineArn,
            `${this.policyEngine.attrPolicyEngineArn}/*`,
            `arn:${this.partition}:bedrock-agentcore:${this.region}:${this.account}:gateway/${config.gatewayName}*`,
          ],
        }),
      ],
    });
    cfnGw.node.addDependency(policyEnginePerms);

    // ───────────────────────── Outputs (exportName 계약 준수) ─────────────────────────
    new CfnOutput(this, 'GatewayUrl', {
      value: this.gateway.gatewayUrl ?? 'n/a',
      description: 'Gateway MCP 엔드포인트 URL (orchestrator gateway 모드 GATEWAY_URL 로 주입)',
      exportName: `${prefix}-gateway-url`,
    });
    new CfnOutput(this, 'GatewayId', {
      value: this.gateway.gatewayId,
      description: 'Gateway ID',
      exportName: `${prefix}-gateway-id`,
    });
    new CfnOutput(this, 'PolicyEngineId', {
      value: this.policyEngine.attrPolicyEngineId,
      description: 'Cedar PolicyEngine ID',
    });
  }

  /**
   * Runtime ID 로 MCP invocations 엔드포인트 URL 을 조립한다(percent-encoded ARN 경로).
   *
   * 형식: https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{URL인코딩ARN}/invocations?qualifier=DEFAULT
   * (orchestrator mcp_client.build_runtime_mcp_url 와 동일 — 거기선 quote(arn, safe="")).
   *
   * ARN = arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtimeId}. 이 중 runtimeId 만
   * CDK 토큰(synth 시 미해석)이고 값은 URL-safe([a-zA-Z0-9_-])이므로, 나머지 ':' '/' 를 미리
   * %3A/%2F 로 치환한 리터럴에 토큰을 결합하면 완전 인코딩된 경로가 배포 시 그대로 해석된다.
   * (전체 ARN 토큰에 encodeURIComponent 를 적용하면 토큰 문자열 자체가 인코딩되지 않아 실패하므로,
   *  이렇게 부분 조립하는 것이 정확하다.)
   */
  private buildRuntimeMcpEndpoint(runtimeId: string): string {
    const encodedArnPrefix = `arn%3Aaws%3Abedrock-agentcore%3A${this.region}%3A${this.account}%3Aruntime%2F`;
    return (
      `https://bedrock-agentcore.${this.region}.amazonaws.com` +
      `/runtimes/${encodedArnPrefix}${runtimeId}/invocations?qualifier=DEFAULT`
    );
  }
}
