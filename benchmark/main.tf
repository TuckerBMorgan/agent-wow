# Independent results service: tofu -chdir=benchmark init && tofu -chdir=benchmark apply
terraform {
  required_providers {
    aws     = { source = "hashicorp/aws", version = "= 6.69.1" }
    archive = { source = "hashicorp/archive", version = "~> 2.7" }
  }
}

variable "aws_region" { default = "us-west-2" }
provider "aws" { region = var.aws_region }

resource "aws_dynamodb_table" "runs" {
  name         = "agent-wow-benchmark-results"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "run_id"
  attribute {
    name = "run_id"
    type = "S"
  }
}

resource "aws_iam_role" "results" {
  name_prefix = "agent-wow-benchmark-"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "results" {
  role = aws_iam_role.results.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.runs.arn
    }]
  })
}

resource "aws_iam_role_policy_attachment" "logs" {
  role       = aws_iam_role.results.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "archive_file" "results" {
  type        = "zip"
  source_file = "${path.module}/../benchmark_results.py"
  output_path = "${path.module}/.terraform/results.zip"
}

resource "aws_lambda_function" "results" {
  function_name    = "agent-wow-benchmark-results"
  role             = aws_iam_role.results.arn
  runtime          = "python3.13"
  handler          = "benchmark_results.handler"
  filename         = data.archive_file.results.output_path
  source_code_hash = data.archive_file.results.output_base64sha256
  timeout          = 10
  environment { variables = { TABLE = aws_dynamodb_table.runs.name } }
  depends_on = [aws_iam_role_policy.results, aws_iam_role_policy_attachment.logs]
}

resource "aws_lambda_function_url" "results" {
  function_name      = aws_lambda_function.results.function_name
  authorization_type = "NONE" # The handler validates the per-run bearer key.
}

resource "aws_lambda_permission" "url" {
  function_name          = aws_lambda_function.results.function_name
  action                 = "lambda:InvokeFunctionUrl"
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "invoke" {
  function_name            = aws_lambda_function.results.function_name
  action                   = "lambda:InvokeFunction"
  principal                = "*"
  invoked_via_function_url = true
}

output "url" { value = aws_lambda_function_url.results.function_url }
output "table" { value = aws_dynamodb_table.runs.name }
output "region" { value = var.aws_region }
