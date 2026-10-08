data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.project}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "task" {
  name               = "${var.project}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid    = "Bedrock"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "DynamoDB"
    effect = "Allow"
    actions = [
      # Repositories probe their table at import time (see
      # repositories/base.py), so without DescribeTable the container exits
      # before it ever serves a request.
      "dynamodb:DescribeTable",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:Scan",
    ]
    resources = [
      var.dynamodb_table_arn,
      "${var.dynamodb_table_arn}/index/*",
      var.artifacts_table_arn,
      "${var.artifacts_table_arn}/index/*",
      var.knowledge_table_arn,
      "${var.knowledge_table_arn}/index/*",
      var.usage_table_arn,
      "${var.usage_table_arn}/index/*",
      var.users_table_arn,
    ]
  }

  statement {
    sid    = "CloudWatchRead"
    effect = "Allow"
    actions = [
      # ListMetrics is not optional here: vended AgentCore series are only
      # queryable with the exact dimension set AWS emits, and partial sets
      # return no datapoints at all. The service discovers the sets first and
      # replays them into GetMetricData, so losing ListMetrics does not make the
      # dashboard wrong — it makes it silently empty.
      "cloudwatch:GetMetricData",
      "cloudwatch:ListMetrics",
      # Span drill-down. StartQuery/GetQueryResults run Logs Insights against
      # whichever span destination this Region uses; DescribeLogGroups is what
      # discovers which of the two exists, and without it the service cannot
      # tell "no spans" from "wrong log group".
      "logs:StartQuery",
      "logs:GetQueryResults",
      "logs:DescribeLogGroups",
      # Describe alarms for sync_agent_alarms.py to check existing alarms.
      "cloudwatch:DescribeAlarms",
    ]
    # Neither action supports resource-level permissions; both are scoped by
    # condition or not at all, which is why this is "*" rather than an ARN list.
    resources = ["*"]
  }

  statement {
    sid    = "CostExplorerRead"
    effect = "Allow"
    actions = [
      # Cost Explorer has no resource-level ARNs, so this cannot be narrowed.
      # It is a read of the account's own bill, filtered in the caller to the
      # AgentCore service.
      "ce:GetCostAndUsage",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "PriceListRead"
    effect = "Allow"
    # The published AgentCore rate card (Runtime vCPU/GB-hour, Gateway per
    # invocation, Memory per event). Free to call; without it the collector
    # prices runtime hours from the pinned constants and says so.
    actions   = ["pricing:GetProducts"]
    resources = ["*"]
  }

  statement {
    sid    = "VendedUsageLogDelivery"
    effect = "Allow"
    # Per-runtime USAGE_LOGS delivery: the server creates a delivery source per
    # runtime and joins it to the stack's destination (modules/observability).
    # Delivery APIs carry no resource ARNs; the AgentCore action is the one the
    # source creation checks against the runtime being delivered from.
    actions = [
      "logs:PutDeliverySource",
      "logs:GetDeliverySource",
      "logs:DescribeDeliverySources",
      "logs:CreateDelivery",
      "logs:GetDelivery",
      "logs:DescribeDeliveries",
      "logs:GetDeliveryDestination",
      "logs:DescribeDeliveryDestinations",
      # The V2 delivery API is backed by the older log-delivery calls and the
      # log group's resource policy; CreateDelivery needs these too.
      "logs:CreateLogDelivery",
      "logs:GetLogDelivery",
      "logs:UpdateLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies",
      "bedrock-agentcore:AllowVendedLogDeliveryForResource",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "AgentCoreVersionHistory"
    effect = "Allow"
    # The backfill reads each runtime's and harness's version list to recover
    # which model an agent ran on a given day.
    actions = [
      "bedrock-agentcore:ListAgentRuntimeVersions",
      "bedrock-agentcore:ListHarnessVersions",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "InferenceProfileManage"
    effect = "Allow"
    actions = [
      "bedrock:CreateInferenceProfile",
      "bedrock:GetInferenceProfile",
      "bedrock:ListInferenceProfiles",
      "bedrock:TagResource",
      "bedrock:ListTagsForResource",
    ]
    resources = ["*"]
  }

  statement {
    sid = "InsightsAlarmWrite"
    actions = [
      "cloudwatch:PutMetricAlarm",
      "cloudwatch:DeleteAlarms",
    ]
    # Scoped to the prefix `sync_agent_alarms.py` owns. This is a shared account
    # with other people's AgentCore workloads; the script must be structurally
    # unable to delete an alarm it did not create.
    resources = [
      "arn:aws:cloudwatch:${var.region}:${data.aws_caller_identity.current.account_id}:alarm:ap-insights-*",
    ]
  }

  statement {
    sid       = "Cognito"
    effect    = "Allow"
    actions   = ["cognito-idp:InitiateAuth"]
    resources = ["*"]
  }

  # The pool signs in by email, so tokens carry only the sub and the usage ledger
  # is keyed by it. Naming a person on the admin Insights views is a read-time
  # ListUsers (`sub = "…"` filter) against this one pool.
  dynamic "statement" {
    for_each = var.user_pool_arn != "" ? [var.user_pool_arn] : []
    content {
      sid       = "CognitoDirectory"
      effect    = "Allow"
      actions   = ["cognito-idp:ListUsers"]
      resources = [statement.value]
    }
  }

  statement {
    sid    = "AgentCore"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:InvokeAgentRuntime",
      "bedrock-agentcore:ListAgentRuntimes",
      "bedrock-agentcore:GetAgentRuntime",
      # Gateways are registered as composable MCP records, so the registry
      # views list and describe them alongside the runtimes.
      "bedrock-agentcore:ListGateways",
      "bedrock-agentcore:GetGateway",
      # The "connect and view tool list" surface signs a SigV4 tools/list to an
      # AWS_IAM gateway with THIS role's credentials, so it needs InvokeGateway.
      # Without it the call returns a bare 403 (the runtime and harness execution
      # roles already have it, which is why agents reach the gateway but the
      # server UI 403s).
      "bedrock-agentcore:InvokeGateway",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "AgentRegistry"
    effect = "Allow"
    # Agent Registry moved to its own `agent-registry` namespace on 2026-08-06;
    # the `bedrock-agentcore:*Registry*` preview actions stop working when that
    # namespace shuts down on 2026-10-30 (AWS registry-faq).
    actions = [
      "agent-registry:CreateRegistry",
      "agent-registry:GetRegistry",
      "agent-registry:UpdateRegistry",
      "agent-registry:ListRegistries",
      "agent-registry:DeleteRegistry",
      "agent-registry:CreateRegistryRecord",
      "agent-registry:GetRegistryRecord",
      "agent-registry:UpdateRegistryRecord",
      "agent-registry:ListRegistryRecords",
      "agent-registry:DeleteRegistryRecord",
      "agent-registry:SubmitRegistryRecordForApproval",
      "agent-registry:UpdateRegistryRecordStatus",
      "agent-registry:SearchDiscoverableRegistryRecords",
      "agent-registry:ListDiscoverableRegistryRecords",
      "agent-registry:GetDiscoverableRegistryRecord",
      "agent-registry:BatchGetDiscoverableRegistryRecord",
    ]
    resources = ["arn:aws:agent-registry:${var.region}:${data.aws_caller_identity.current.account_id}:*"]
  }

  statement {
    sid    = "AgentCoreHarness"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:CreateHarness",
      "bedrock-agentcore:GetHarness",
      "bedrock-agentcore:ListHarnesses",
      "bedrock-agentcore:UpdateHarness",
      "bedrock-agentcore:DeleteHarness",
      "bedrock-agentcore:InvokeHarness",
      # Harness APIs authorize against the underlying runtime and memory
      # resources as well as the harness itself.
      "bedrock-agentcore:CreateAgentRuntime",
      "bedrock-agentcore:UpdateAgentRuntime",
      "bedrock-agentcore:DeleteAgentRuntime",
      # CreateHarness builds a companion runtime and gives it a DEFAULT endpoint,
      # both authorized against the caller.
      "bedrock-agentcore:CreateAgentRuntimeEndpoint",
      "bedrock-agentcore:UpdateAgentRuntimeEndpoint",
      "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
      "bedrock-agentcore:CreateMemory",
      "bedrock-agentcore:UpdateMemory",
      "bedrock-agentcore:DeleteMemory",
      # CreateHarness polls the memory it just created until it is ACTIVE, so the
      # read actions are needed too — without GetMemory, harness creation fails
      # with AccessDenied *after* the memory exists, leaving it orphaned.
      "bedrock-agentcore:GetMemory",
      "bedrock-agentcore:ListMemories",
      # CreateHarness is called with cost-allocation tags (Platform/AgentName,
      # see harness_service), which authorizes TagResource against the harness
      # AND the companion runtime/memory the tags propagate to — hence "*", not
      # just harness/*. Without it CreateHarness fails with AccessDeniedException.
      "bedrock-agentcore:TagResource",
      "bedrock-agentcore:UntagResource",
      "bedrock-agentcore:ListTagsForResource",
    ]
    resources = ["*"]
  }

  statement {
    sid = "EvaluationsRead"
    actions = [
      "bedrock-agentcore:ListEvaluators",
      "bedrock-agentcore:GetEvaluator",
      "bedrock-agentcore:ListBatchEvaluations",
      "bedrock-agentcore:GetBatchEvaluation",
    ]
    resources = ["*"]
  }

  statement {
    sid = "EvaluationsRun"
    actions = [
      # Charged per judge model call, so the route behind this is admin-only.
      "bedrock-agentcore:StartBatchEvaluation",
      "bedrock-agentcore:StopBatchEvaluation",
    ]
    resources = ["*"]
  }

  statement {
    # StartBatchEvaluation writes its per-turn results to CloudWatch Logs *as the
    # caller* (forward access session), creating the results log group on first
    # use. Without this the call is rejected with "FAS credentials do not have
    # permission to create CloudWatch log groups" even though the
    # bedrock-agentcore actions above are granted.
    sid = "EvaluationsResultsLogs"
    actions = [
      "logs:CreateLogGroup",
      "logs:PutRetentionPolicy",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = [
      "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/evaluations/*",
      "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/evaluations/*:log-stream:*",
    ]
  }

  statement {
    sid    = "PrefsTableWrite"
    effect = "Allow"
    actions = [
      # Per-user preferences (dashboard layout, etc.). One keyed get and one
      # keyed put — the access pattern is deterministic and small. Query, Scan and
      # DeleteItem are excluded; a narrower grant is a cheap guard against a later
      # Scan that would be a mistake.
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      # Required, and not optional: `DynamoDBRepository.__init__` calls
      # `_ensure_table_exists()`, which is `table.load()`, which is DescribeTable.
      # Without it the repository raises at **import** time and the whole server
      # crash-loops — measured on 2026-08-16, when task definition 37 started
      # zero tasks and the previous revision kept serving.
      "dynamodb:DescribeTable",
    ]
    resources = [
      var.prefs_table_arn,
    ]
  }

  statement {
    sid    = "AgentCoreHarnessCommand"
    effect = "Allow"
    actions = [
      # Runs a shell command inside a harness session's own container, which is
      # the only way a file the agent produced can reach the user: a harness
      # executes its tools inside AWS, so nothing it writes passes through this
      # server. See services/harness_output_service.py.
      #
      # Scoped to harnesses on purpose. This action also accepts a plain runtime
      # ARN, and the server has no reason to run commands inside ordinary runtime
      # agents' containers.
      #
      # Deliberately NOT granted to the harness execution role: an agent able to
      # run commands in its own container makes `allowedTools` meaningless. No
      # route exposes command execution either — the only command strings are
      # constants in services/harness_sandbox_scripts.py.
      "bedrock-agentcore:InvokeAgentRuntimeCommand",
    ]
    resources = [
      "arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:harness/*",
    ]
  }

  statement {
    sid       = "PassHarnessExecutionRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [var.harness_execution_role_arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["bedrock-agentcore.amazonaws.com"]
    }
  }

  statement {
    sid    = "SkillsBucketWrite"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = [
      var.skills_bucket_arn,
      "${var.skills_bucket_arn}/*",
    ]
  }

  statement {
    sid    = "ArtifactsBucketWrite"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = [
      var.artifacts_bucket_arn,
      "${var.artifacts_bucket_arn}/*",
    ]
  }

  statement {
    sid    = "KnowledgeBases"
    effect = "Allow"
    actions = [
      "bedrock:CreateKnowledgeBase",
      "bedrock:GetKnowledgeBase",
      "bedrock:ListKnowledgeBases",
      "bedrock:DeleteKnowledgeBase",
      "bedrock:CreateDataSource",
      "bedrock:GetDataSource",
      "bedrock:ListDataSources",
      "bedrock:DeleteDataSource",
      "bedrock:IngestKnowledgeBaseDocuments",
      "bedrock:GetKnowledgeBaseDocuments",
      "bedrock:ListKnowledgeBaseDocuments",
      "bedrock:DeleteKnowledgeBaseDocuments",
      # IngestKnowledgeBaseDocuments starts an ingestion job of its own, and the
      # authorization for that is checked against the caller: without this an
      # upload fails with AccessDeniedException on StartIngestionJob.
      "bedrock:StartIngestionJob",
      # Syncing a bucket-backed knowledge base: the job is read to report progress
      # and to avoid starting a second one, and stopped so a knowledge base can be
      # deleted mid-sync. Missing these fails quietly — the sync itself runs, but
      # the panel reports no progress at all.
      "bedrock:ListIngestionJobs",
      "bedrock:GetIngestionJob",
      "bedrock:StopIngestionJob",
    ]
    # CreateKnowledgeBase has no resource to scope to — the knowledge base does
    # not exist yet — and the per-KB boundary is the server's ownership check.
    resources = ["*"]
  }

  statement {
    sid    = "KnowledgeGateways"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:CreateGateway",
      "bedrock-agentcore:DeleteGateway",
      "bedrock-agentcore:CreateGatewayTarget",
      "bedrock-agentcore:GetGatewayTarget",
      "bedrock-agentcore:ListGatewayTargets",
      "bedrock-agentcore:DeleteGatewayTarget",
      # Creating a gateway implicitly creates a workload identity in the
      # account's single identity directory, and deleting one removes it.
      # Without these the CreateGateway call fails with a 403 from
      # AgentCredentialProvider, not from the gateway API itself.
      "bedrock-agentcore:CreateWorkloadIdentity",
      "bedrock-agentcore:GetWorkloadIdentity",
      "bedrock-agentcore:DeleteWorkloadIdentity",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "PassKnowledgeServiceRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [var.kb_service_role_arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["bedrock.amazonaws.com"]
    }
  }

  statement {
    sid       = "PassKnowledgeGatewayRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [var.kb_gateway_role_arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["bedrock-agentcore.amazonaws.com"]
    }
  }

  statement {
    sid    = "KnowledgeBucketWrite"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = [
      var.knowledge_bucket_arn,
      "${var.knowledge_bucket_arn}/*",
    ]
  }

  # Managed S3 sources: the server puts site uploads under users/{sub}/{kb_key}/
  # and deletes that prefix with the knowledge base. External source buckets are
  # read by the kb-service role only — the server never writes to them, and the
  # absence of a grant here is what enforces that.
  dynamic "statement" {
    for_each = var.knowledge_source_bucket_arn != "" ? [1] : []
    content {
      sid    = "KnowledgeSourceBucketWrite"
      effect = "Allow"
      actions = [
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject",
        "s3:ListBucket",
      ]
      resources = [
        var.knowledge_source_bucket_arn,
        "${var.knowledge_source_bucket_arn}/*",
      ]
    }
  }

  # Browser screenshots, read only. The bucket belongs to the built-in-tools
  # gateway (infra/builtin_tools_gateway/deploy.py), whose Lambda writes the PNGs;
  # the server only reads one back when a stored thread is reopened and the
  # Lambda's presigned URL has expired. No write and no list: the key is looked up
  # in the thread, never discovered by scanning the bucket, and a grant to list
  # would expose the session labels of every other caller.
  dynamic "statement" {
    for_each = var.browser_screenshot_bucket_arn != "" ? [1] : []
    content {
      sid       = "BrowserScreenshotsRead"
      effect    = "Allow"
      actions   = ["s3:GetObject"]
      resources = ["${var.browser_screenshot_bucket_arn}/screenshots/*"]
    }
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${var.project}-ecs-task-policy"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}
