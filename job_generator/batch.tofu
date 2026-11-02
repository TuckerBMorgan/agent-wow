locals {
  runner_image_digest = jsondecode(file("${path.module}/benchmark.json")).runner_image_digest
  client_image_digest = jsondecode(file("${path.module}/benchmark.json")).client_image_digest
}

resource "aws_launch_template" "client" {
  name_prefix                          = "agent-wow-client-"
  image_id                             = "ami-09f02015912e773f7"
  instance_type                        = "m6i.xlarge"
  instance_initiated_shutdown_behavior = "terminate"

  iam_instance_profile {
    name = aws_iam_instance_profile.client.name
  }

  network_interfaces {
    device_index                = 0
    associate_public_ip_address = true
    subnet_id                   = aws_subnet.batch.id
    security_groups             = [aws_security_group.client.id]
  }

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      delete_on_termination = true
      encrypted             = true
      volume_size           = 80
      volume_type           = "gp3"
    }
  }
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
    max_vcpus           = 128
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
      { name = "JOBS_TABLE", value = aws_dynamodb_table.jobs.name },
      { name = "CLIENT_REPOSITORY", value = aws_ecr_repository.runner.repository_url },
      { name = "CLIENT_IMAGE_DIGEST", value = local.client_image_digest },
      { name = "CLIENT_LAUNCH_TEMPLATE_ID", value = aws_launch_template.client.id },
      { name = "CLIENT_LAUNCH_TEMPLATE_VERSION", value = tostring(aws_launch_template.client.latest_version) },
      { name = "CLIENT_ROLE_ID", value = aws_iam_role.client.unique_id }
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
