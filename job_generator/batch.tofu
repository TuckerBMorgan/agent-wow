locals {
  runner_image_digest = jsondecode(file("${path.module}/benchmark.json")).runner_image_digest
}

resource "aws_launch_template" "batch" {
  name_prefix = "agent-wow-"

  block_device_mappings {
    device_name = "/dev/xvda"

    ebs {
      delete_on_termination = true
      encrypted             = true
      volume_size           = 200
      volume_type           = "gp3"
    }
  }
}

resource "aws_batch_compute_environment" "runner" {
  name = "agent-wow-runner"
  type = "MANAGED"

  compute_resources {
    allocation_strategy = "BEST_FIT_PROGRESSIVE"
    instance_role       = aws_iam_instance_profile.batch_instance.arn
    instance_type       = ["m6i.2xlarge"]
    max_vcpus           = 8
    security_group_ids  = [aws_security_group.batch_runner.id]
    subnets             = [aws_subnet.batch.id]
    type                = "EC2"

    ec2_configuration {
      image_type = "ECS_AL2023"
    }

    launch_template {
      launch_template_id = aws_launch_template.batch.id
      version            = "$Latest"
    }
  }

  depends_on = [aws_iam_role_policy_attachment.batch_instance]
}

resource "aws_batch_job_queue" "runner" {
  name     = "agent-wow-runner"
  priority = 1
  state    = "ENABLED"

  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.runner.arn
  }
}

resource "aws_batch_job_definition" "runner" {
  name = "agent-wow-runner"
  type = "container"

  container_properties = jsonencode({
    image            = "${aws_ecr_repository.runner.repository_url}@${local.runner_image_digest}"
    privileged       = true
    jobRoleArn       = aws_iam_role.runner_job.arn
    executionRoleArn = aws_iam_role.runner_execution.arn
    environment = [
      { name = "ARTIFACTS_BUCKET", value = aws_s3_bucket.artifacts.id },
      { name = "JOBS_TABLE", value = aws_dynamodb_table.jobs.name }
    ]
    resourceRequirements = [
      { type = "VCPU", value = "8" },
      { type = "MEMORY", value = "28672" }
    ]
  })

  timeout {
    attempt_duration_seconds = 28800
  }
}
