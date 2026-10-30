data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "benchmark" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "agent-wow" }
}

resource "aws_internet_gateway" "benchmark" {
  vpc_id = aws_vpc.benchmark.id
}

resource "aws_subnet" "batch" {
  vpc_id                  = aws_vpc.benchmark.id
  availability_zone       = data.aws_availability_zones.available.names[0]
  cidr_block              = "10.42.0.0/24"
  map_public_ip_on_launch = true

  tags = { Name = "agent-wow-batch" }
}

resource "aws_route_table" "batch" {
  vpc_id = aws_vpc.benchmark.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.benchmark.id
  }
}

resource "aws_route_table_association" "batch" {
  subnet_id      = aws_subnet.batch.id
  route_table_id = aws_route_table.batch.id
}

resource "aws_security_group" "batch_runner" {
  name   = "agent-wow-batch-runner"
  vpc_id = aws_vpc.benchmark.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

output "batch_subnet_ids" {
  value = [aws_subnet.batch.id]
}

output "batch_security_group_ids" {
  value = [aws_security_group.batch_runner.id]
}
