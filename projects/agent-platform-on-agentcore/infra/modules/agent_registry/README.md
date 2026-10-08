# agent_registry

Creates the AgentCore Agent Registry used by the `/registry` page and injects its
id into the server as `AGENT_REGISTRY_ID`.

## Why this module shells out to Python

Agent Registry has no native IaC support:

- No CloudFormation resource type (`AWS::BedrockAgentCore::*` covers Runtime,
  Gateway, Memory, and others — but not Registry), so `awscc` has no
  `awscc_bedrockagentcore_registry`.
- No `aws` provider resource.
- Not even in the AWS CLI (`aws agent-registry-control create-registry` is an
  invalid choice).

boto3 is the only surface that exposes `CreateRegistry`, so the lifecycle runs
through `null_resource` provisioners calling `scripts/registry.py`, with a
`data.external` lookup feeding the outputs.

## Behaviour

- `create` is idempotent: an existing registry with the same name is adopted
  rather than duplicated, so re-running `apply` after a failure is safe.
- `CreateRegistry` is asynchronous; the script polls until the registry is `READY`.
- `destroy` deletes the registry's records first, since a non-empty registry
  cannot be deleted.
- `auto_approval = true` (default) means a record is approved as soon as it is
  submitted. Records are still created as `DRAFT` and must be submitted — the
  server does that automatically on create.

## Caveats

- The registry is not tracked as a real Terraform resource, so drift (e.g. an
  out-of-band description change) is not detected. `terraform destroy` does
  delete it.
- Registry APIs live in the `agent-registry` namespace (control plane
  `agent-registry-control`, served at the `.api.aws` endpoint).
- Requires `python3` with `boto3` (botocore >= 1.43, which knows the
  `agent-registry-control` model) on whoever runs `terraform apply`; point
  `python_bin` at a venv python where the system one is too old.
