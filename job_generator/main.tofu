terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}
variable "table_name" { default = "agent-wow-jobs" }
resource "aws_dynamodb_table" "jobs" {
  name = var.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key = "pk"
  attribute {
    name = "pk"
    type = "S"
  }
}
output "table_name" { value = aws_dynamodb_table.jobs.name }
