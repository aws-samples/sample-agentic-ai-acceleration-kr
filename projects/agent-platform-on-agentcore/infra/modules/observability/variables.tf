variable "project" {
  description = "Stack prefix; names the log group and delivery destination."
  type        = string
}

variable "retention_in_days" {
  description = "How long session usage rows are kept. The collector folds them into DynamoDB within minutes; the rows only matter for a backfill."
  type        = number
  default     = 90
}
