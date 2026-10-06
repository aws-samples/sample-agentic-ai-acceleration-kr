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

# AgentCore Registry is preview-only: no CloudFormation type and no aws/awscc
# resource exist, so the lifecycle is driven through boto3.
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
    ])
  }

  provisioner "local-exec" {
    when    = destroy
    command = self.triggers.delete_command
  }
}

# Sync APPROVE_ALL on/off without destroying the registry.
resource "null_resource" "registry_approval" {
  depends_on = [null_resource.registry]

  triggers = {
    auto_approval = tostring(var.auto_approval)
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
