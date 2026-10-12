# agent_registry

Creates the AWS Agent Registry used by the `/registry` page and injects its id
into the server as `AGENT_REGISTRY_ID`. Also owns the pieces that sit next to
the registry: the custom metadata schema, the synchronisation role, and the
approval-workflow event route.

## Why this module still shells out to Python

AWS now has native IaC for the registry — `AWS::AgentRegistry::Registry` /
`AWS::AgentRegistry::RegistryRecord` in CloudFormation and
`aws_agentregistry_registry` in the aws provider (6.64.0, 2026-09-09; 6.65.0
added auto-detection and encryption). This stack pins `aws ~> 5`, and the
registry it manages was adopted by name rather than created by a resource, so
moving over means a provider major bump plus an `import` of the live registry.
Until then the lifecycle runs through `null_resource` provisioners calling
`scripts/registry.py` (boto3 against `agent-registry-control` at the `.api.aws`
endpoint), with a `data.external` lookup feeding the outputs. There is no
Terraform resource for *records*; CloudFormation has one.

## Behaviour

- `create` is idempotent: an existing registry with the same name is adopted
  rather than duplicated, so re-running `apply` after a failure is safe.
- `CreateRegistry` is asynchronous; the script polls until the registry is `READY`.
- `destroy` deletes the registry's records first, since a non-empty registry
  cannot be deleted.
- `auto_approval = true` (default) means a record is approved as soon as it is
  submitted. Records are still created as `DRAFT` and must be submitted — the
  server does that automatically on create.
- `custom_metadata_schema` (record type => JSON Schema string, `"DEFAULT"` for
  the default schema) is set on create and re-synced on every apply through
  `UpdateRegistry`. AWS only accepts additive changes — a saved field or enum
  value can never be removed or retyped — so the script merges the desired
  schema over the live one (fields and enum values the registry already has are
  kept) and a change AWS still rejects, such as a retype, is reported on stderr
  while the apply continues. The default schema keeps every field optional
  (`owner`, `team`, `tier`); a required field would break resubmission of every
  existing record. `owner` is written by the server with the registering user.
- `kms_key_arn` is creation-time only in AWS and is deliberately *not* a
  replacement trigger: replacing `null_resource.registry` runs `delete` on the
  live registry and its records.
- `create_sync_role` creates `<project>-registry-sync`, the role AWS assumes to
  SigV4-sign record synchronisation fetches against servers on AgentCore
  Runtime/Gateway (`InvokeGateway`, `InvokeAgentRuntime`). The server receives
  it as `REGISTRY_SYNC_ROLE_ARN` and needs `iam:PassRole` on it (iam module).
- `enable_approval_events` routes `aws.agent-registry` record events (Pending
  Approval, Rejected, Deprecated) for this registry to an SNS topic, with an
  optional email subscription. The registry's own lifecycle events are not
  routed.
- `mcp_endpoint` is the registry's MCP server
  (`https://agent-registry.<region>.api.aws/registry/<id>/mcp`); the gateway
  module attaches it as a target so agents can search the catalog at run time.

## Caveats

- `null_resource.registry`'s destroy provisioner deletes **by name**. Any
  address move or `script_sha` change makes Terraform replace the resource,
  and the delete runs against whatever registry currently has that name. Run
  `terraform state rm 'module.agent_registry[0].null_resource.registry'` first
  and let the create step adopt the existing registry (see the registry memory
  notes for the 2026-09-12 and 2026-10-01 incidents).
- The registry is not tracked as a real Terraform resource, so drift (e.g. an
  out-of-band description change) is not detected. `terraform destroy` does
  delete it.
- Registry APIs live in the `agent-registry` namespace (control plane
  `agent-registry-control`, served at the `.api.aws` endpoint). The
  `bedrock-agentcore` preview namespace shuts down on 2026-10-30; a registry
  created there is not visible from here.
- Requires `python3` with `boto3` (botocore >= 1.43, which knows the
  `agent-registry-control` model) on whoever runs `terraform apply`; point
  `python_bin` at a venv python where the system one is too old.
