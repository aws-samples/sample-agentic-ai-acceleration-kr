variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "api_key_providers" {
  type = map(object({
    display_name = string
    description  = string
  }))
  description = "Map of provider_name -> {display_name, description} for each search engine with API keys"
  default     = {}
}
