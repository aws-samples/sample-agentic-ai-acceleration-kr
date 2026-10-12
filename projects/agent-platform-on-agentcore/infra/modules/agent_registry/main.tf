terraform {
  required_providers {
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}

locals {
  script = "${path.module}/scripts/registry.py"
  # Terraform destroy provisioners cannot reference variables, so the command is
  # frozen into triggers at create time.
  registry_name = "${var.project}-registry"
}

# The registry itself is still driven through boto3. AWS now ships
# AWS::AgentRegistry::Registry / RegistryRecord and the aws provider has
# aws_agentregistry_registry (6.64+), but this stack pins aws ~> 5 and the
# null_resource below adopts an existing registry by name — the migration is a
# provider major bump plus an import, tracked in the module README.
#
# auto_approval is intentionally NOT in these triggers. Changing it used to
# replace this resource, which ran `delete` on destroy and could wipe every
# registry record. Approval policy is synced by null_resource.registry_approval
# below (create-only; adopts an existing registry and PATCHes approval).
resource "null_resource" "registry" {
  triggers = {
    name        = local.registry_name
    description = var.description
    region      = var.region
    # Captured for the destroy-time provisioner, which cannot read vars.
    # Prefer var.python_bin so destroy uses the same boto3 that knows
    # agent-registry-control (system python3 often does not).
    delete_command = "${var.python_bin} ${abspath(local.script)} delete --name ${local.registry_name} --region ${var.region}"
  }

  provisioner "local-exec" {
    command = join(" ", [
      var.python_bin, local.script, "create",
      "--name", local.registry_name,
      "--region", var.region,
      "--description", "'${var.description}'",
      "--auto-approval", tostring(var.auto_approval),
      # Creation-time only (AWS); the adopt path ignores it.
      "--kms-key-arn", "'${var.kms_key_arn}'",
      "--custom-metadata-schema-json", "'${jsonencode(var.custom_metadata_schema)}'",
    ])
  }

  provisioner "local-exec" {
    when    = destroy
    command = self.triggers.delete_command
  }
}

# Sync APPROVE_ALL and the custom metadata schema without destroying the
# registry. The schema is additive-only on the AWS side: a change that removes
# or retypes a saved field is rejected and reported, not applied.
resource "null_resource" "registry_approval" {
  depends_on = [null_resource.registry]

  triggers = {
    auto_approval = tostring(var.auto_approval)
    schema_sha    = sha256(jsonencode(var.custom_metadata_schema))
    script_sha    = filesha256(local.script)
    # Re-run after the registry resource itself is recreated.
    registry_id = null_resource.registry.id
  }

  provisioner "local-exec" {
    command = join(" ", [
      var.python_bin, local.script, "create",
      "--name", local.registry_name,
      "--region", var.region,
      "--auto-approval", tostring(var.auto_approval),
      "--custom-metadata-schema-json", "'${jsonencode(var.custom_metadata_schema)}'",
    ])
  }
}

data "external" "registry" {
  depends_on = [null_resource.registry, null_resource.registry_approval]

  program = [
    var.python_bin, local.script, "lookup",
    "--name", local.registry_name,
    "--region", var.region,
  ]
}

# --- Synchronisation role ------------------------------------------------------
#
# A record can carry `source.fromUrl`; AWS re-fetches the MCP server or agent
# card definition from that URL. Servers on AgentCore Runtime/Gateway need the
# fetch SigV4-signed, which AWS does by assuming a role the caller passes
# (iam:PassRole, PassedToService bedrock-agentcore — AgentCore Identity performs
# the fetch). This is that role; the server task role gets PassRole on it.

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "sync_assume" {
  count = var.create_sync_role ? 1 : 0
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type = "Service"
      # Both principals AWS documents around sync: Identity does the fetch, the
      # registry service principal appears in the migration FAQ.
      identifiers = ["bedrock-agentcore.amazonaws.com", "agent-registry.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "sync" {
  count              = var.create_sync_role ? 1 : 0
  name               = "${var.project}-registry-sync"
  assume_role_policy = data.aws_iam_policy_document.sync_assume[0].json
}

resource "aws_iam_role_policy" "sync" {
  count = var.create_sync_role ? 1 : 0
  name  = "${var.project}-registry-sync"
  role  = aws_iam_role.sync[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # What a sync target on AgentCore accepts: a gateway's /mcp or a
        # runtime's /invocations, both in this account.
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:InvokeGateway",
          "bedrock-agentcore:InvokeAgentRuntime",
        ]
        Resource = ["arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:*"]
      },
    ]
  })
}

# --- Approval-workflow events ---------------------------------------------------
#
# The registry publishes record state transitions to the default EventBridge bus
# (source aws.agent-registry). With auto-approval off, "Pending Approval" is the
# curator's cue; Rejected/Deprecated tell publishers their record left discovery.

resource "aws_sns_topic" "approvals" {
  count = var.enable_approval_events ? 1 : 0
  name  = "${var.project}-registry-approvals"
}

resource "aws_sns_topic_subscription" "approvals_email" {
  count     = var.enable_approval_events && var.approval_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.approvals[0].arn
  protocol  = "email"
  endpoint  = var.approval_email
}

data "aws_iam_policy_document" "approvals_topic" {
  count = var.enable_approval_events ? 1 : 0
  statement {
    sid     = "AllowEventBridgePublish"
    actions = ["sns:Publish"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    resources = [aws_sns_topic.approvals[0].arn]
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.record_state[0].arn]
    }
  }
}

resource "aws_sns_topic_policy" "approvals" {
  count  = var.enable_approval_events ? 1 : 0
  arn    = aws_sns_topic.approvals[0].arn
  policy = data.aws_iam_policy_document.approvals_topic[0].json
}

resource "aws_cloudwatch_event_rule" "record_state" {
  count       = var.enable_approval_events ? 1 : 0
  name        = "${var.project}-registry-record-state"
  description = "AWS Agent Registry record approval-workflow transitions for ${local.registry_name}."
  event_pattern = jsonencode({
    source = ["aws.agent-registry"]
    # The detail-type names changed with the agent-registry namespace; these are
    # the GA values (registry-eventbridge). The registry's own lifecycle events
    # (Registry Ready, …) are deliberately not routed: terraform owns that.
    detail-type = [
      "Registry Record State changed to Pending Approval",
      "Registry Record State changed to Rejected",
      "Registry Record State changed to Deprecated",
    ]
    detail = {
      registryId = [data.external.registry.result.registry_id]
    }
  })
}

resource "aws_cloudwatch_event_target" "record_state_sns" {
  count     = var.enable_approval_events ? 1 : 0
  rule      = aws_cloudwatch_event_rule.record_state[0].name
  target_id = "sns"
  arn       = aws_sns_topic.approvals[0].arn
  input_transformer {
    input_paths = {
      type     = "$.detail-type"
      record   = "$.detail.registryRecordId"
      registry = "$.detail.registryId"
      arn      = "$.resources[0]"
    }
    input_template = "\"<type>: record <record> in registry <registry> (<arn>)\""
  }
}
