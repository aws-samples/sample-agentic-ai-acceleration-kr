# A secret *container* only. The value is written out-of-band (CLI / seed script)
# so the API key never passes through Terraform variables, state, or plan files.
resource "aws_secretsmanager_secret" "this" {
  name                    = var.name
  description             = var.description
  recovery_window_in_days = 0

  tags = merge({ Component = "tool-secret" }, var.tags)
}
