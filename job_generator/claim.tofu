data "archive_file" "claim" {
  type        = "zip"
  source_file = "${path.module}/../claim_api/claim.py"
  output_path = "${path.module}/claim.zip"
}

resource "aws_iam_role" "claim" {
  name = "agent-wow-claim"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "claim_logs" {
  role       = aws_iam_role.claim.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "claim" {
  name = "agent-wow-claim-access"
  role = aws_iam_role.claim.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "dynamodb:UpdateItem"
        Resource = aws_dynamodb_table.jobs.arn
      },
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:CreateSecret",
          "secretsmanager:DeleteSecret",
          "secretsmanager:TagResource"
        ]
        Resource = local.job_secret_arn
      },
      {
        Effect   = "Allow"
        Action   = "batch:SubmitJob"
        Resource = [aws_batch_job_queue.runner.arn, aws_batch_job_definition.runner.arn]
      },
      {
        Effect   = "Allow"
        Action   = "batch:TerminateJob"
        Resource = "arn:${data.aws_partition.current.partition}:batch:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:job/*"
      }
    ]
  })
}

resource "aws_lambda_function" "claim" {
  function_name    = "agent-wow-claim"
  filename         = data.archive_file.claim.output_path
  source_code_hash = data.archive_file.claim.output_base64sha256
  role             = aws_iam_role.claim.arn
  handler          = "claim.handler"
  runtime          = "python3.12"
  timeout          = 30

  environment {
    variables = {
      BATCH_JOB_DEFINITION = aws_batch_job_definition.runner.arn
      BATCH_JOB_QUEUE      = aws_batch_job_queue.runner.arn
      JOBS_TABLE           = aws_dynamodb_table.jobs.name
    }
  }

  depends_on = [
    aws_iam_role_policy.claim,
    aws_iam_role_policy_attachment.claim_logs
  ]
}

resource "aws_apigatewayv2_api" "claim" {
  name          = "agent-wow-claim"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "claim" {
  api_id                 = aws_apigatewayv2_api.claim.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.claim.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "claim" {
  api_id    = aws_apigatewayv2_api.claim.id
  route_key = "POST /claim"
  target    = "integrations/${aws_apigatewayv2_integration.claim.id}"
}

resource "aws_apigatewayv2_stage" "claim" {
  api_id      = aws_apigatewayv2_api.claim.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = 10
    throttling_rate_limit  = 5
  }
}

resource "aws_lambda_permission" "claim" {
  statement_id  = "AllowClaimApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.claim.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.claim.execution_arn}/*/POST/claim"
}

output "claim_url" {
  value = "${aws_apigatewayv2_api.claim.api_endpoint}/claim"
}
