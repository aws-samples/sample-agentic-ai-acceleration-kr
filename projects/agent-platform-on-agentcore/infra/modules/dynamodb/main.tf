resource "aws_dynamodb_table" "threads" {
  deletion_protection_enabled = !var.allow_destroy
  point_in_time_recovery {
    enabled = true
  }

  name         = var.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "thread_id"

  attribute {
    name = "thread_id"
    type = "S"
  }
}

# Each artifact version is its own item, so the panel can list v1..vN and share a
# specific one. The GSI backs "every artifact in this thread".
resource "aws_dynamodb_table" "artifacts" {
  deletion_protection_enabled = !var.allow_destroy
  point_in_time_recovery {
    enabled = true
  }

  name         = var.artifacts_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "artifact_id"
  range_key    = "version"

  attribute {
    name = "artifact_id"
    type = "S"
  }

  attribute {
    name = "version"
    type = "N"
  }

  attribute {
    name = "thread_id"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  global_secondary_index {
    name            = "thread_id-created_at-index"
    hash_key        = "thread_id"
    range_key       = "created_at"
    projection_type = "ALL"
  }
}

# One item per user-created knowledge base. The item is written *before*
# CreateKnowledgeBase runs and carries the id of every AWS resource the
# provisioner has made so far, so a container restart mid-provisioning resumes
# instead of orphaning resources nothing knows about.
resource "aws_dynamodb_table" "knowledge_bases" {
  deletion_protection_enabled = !var.allow_destroy
  point_in_time_recovery {
    enabled = true
  }

  name         = var.knowledge_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "kb_key"

  attribute {
    name = "kb_key"
    type = "S"
  }

  attribute {
    name = "owner_id"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  # "My knowledge bases" is the common read, and it must be a query rather than
  # a scan of everyone's.
  global_secondary_index {
    name            = "owner_id-created_at-index"
    hash_key        = "owner_id"
    range_key       = "created_at"
    projection_type = "ALL"
  }
}

resource "aws_dynamodb_table" "usage" {
  deletion_protection_enabled = !var.allow_destroy
  point_in_time_recovery {
    enabled = true
  }

  name         = var.usage_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }
}

# Per-user preferences (dashboard layout, etc.). One item per user per preference
# name, storing the entire value under a single `value` attribute. Unlike the
# usage table (ADD-only counters), this is a whole-value put_item, so it must
# have its own table to avoid violating the usage table's invariant that
# concurrent writes never lose increments.
resource "aws_dynamodb_table" "prefs" {
  deletion_protection_enabled = !var.allow_destroy
  point_in_time_recovery {
    enabled = true
  }

  name         = var.prefs_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }
}

# Who signed in through an OIDC provider (Microsoft Entra ID), written on each
# login (server/repositories/user_repository.py). Cognito users need no row —
# the pool is their directory (ListUsers). Entra users have no pool here, and
# the usage ledger keys people by `sub`, so without this the admin Insights
# views could only show their sub. Not read on request paths; roles come from
# the token. pk = "<provider>#<sub>".
resource "aws_dynamodb_table" "users" {
  deletion_protection_enabled = !var.allow_destroy
  point_in_time_recovery {
    enabled = true
  }

  name         = var.users_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }
}
