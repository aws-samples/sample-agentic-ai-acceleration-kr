output "table_name" {
  value = aws_dynamodb_table.threads.name
}

output "table_arn" {
  value = aws_dynamodb_table.threads.arn
}

output "artifacts_table_name" {
  value = aws_dynamodb_table.artifacts.name
}

output "artifacts_table_arn" {
  value = aws_dynamodb_table.artifacts.arn
}

output "knowledge_table_name" {
  value = aws_dynamodb_table.knowledge_bases.name
}

output "knowledge_table_arn" {
  value = aws_dynamodb_table.knowledge_bases.arn
}

output "usage_table_name" {
  value = aws_dynamodb_table.usage.name
}

output "usage_table_arn" {
  value = aws_dynamodb_table.usage.arn
}

output "prefs_table_name" {
  value = aws_dynamodb_table.prefs.name
}

output "prefs_table_arn" {
  value = aws_dynamodb_table.prefs.arn
}

output "users_table_name" {
  value = aws_dynamodb_table.users.name
}

output "users_table_arn" {
  value = aws_dynamodb_table.users.arn
}

output "teams_table_name" {
  value = aws_dynamodb_table.teams.name
}

output "teams_table_arn" {
  value = aws_dynamodb_table.teams.arn
}

output "slack_table_name" {
  value = aws_dynamodb_table.slack.name
}

output "slack_table_arn" {
  value = aws_dynamodb_table.slack.arn
}
