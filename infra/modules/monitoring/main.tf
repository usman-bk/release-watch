terraform {
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.72"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
  }
}

variable "name" {
  type = string
}

variable "region" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "vpc_cidr" {
  type = string
}

variable "subnet_id" {
  type = string
}

variable "prometheus_image" {
  type = string
}

variable "alertmanager_image" {
  type = string
}

variable "grafana_image" {
  type = string
}

variable "grafana_password_version" {
  type = number
}

variable "instance_type" {
  type    = string
  default = "t4g.small"
}

variable "data_volume_gib" {
  type    = number
  default = 50
}

locals {
  operator_secrets = toset(["pager_url", "ticket_url", "heartbeat_url"])
}

data "aws_ssm_parameter" "ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

data "aws_subnet" "this" {
  id = var.subnet_id
}

# No ingress rules. People reach Prometheus and Grafana through an SSM port forward.
resource "aws_security_group" "this" {
  name_prefix = "${var.name}-monitoring-"
  description = "Prometheus, Alertmanager and Grafana host"
  vpc_id      = var.vpc_id

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_egress_rule" "scrape" {
  security_group_id = aws_security_group.this.id
  description       = "Scrape service tasks"
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 8000
  to_port           = 8000
}

resource "aws_vpc_security_group_egress_rule" "dns" {
  security_group_id = aws_security_group.this.id
  description       = "Resolve service discovery names"
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "udp"
  from_port         = 53
  to_port           = 53
}

#trivy:ignore:AVD-AWS-0104 The host pulls images, reads secrets and calls webhooks over HTTPS.
resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.this.id
  description       = "AWS APIs, images and alert webhooks"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

#trivy:ignore:AVD-AWS-0098 The default AWS key is enough for a demo; a customer key adds cost.
resource "aws_secretsmanager_secret" "operator" {
  for_each = local.operator_secrets
  name     = "${var.name}/${each.key}"
}

#trivy:ignore:AVD-AWS-0098 The default AWS key is enough for a demo; a customer key adds cost.
resource "aws_secretsmanager_secret" "grafana_password" {
  name = "${var.name}/grafana_password"
}

ephemeral "random_password" "grafana" {
  length  = 32
  special = false
}

# Write-only, so the password is never stored in the Terraform state.
resource "aws_secretsmanager_secret_version" "grafana_password" {
  secret_id                = aws_secretsmanager_secret.grafana_password.id
  secret_string_wo         = ephemeral.random_password.grafana.result
  secret_string_wo_version = var.grafana_password_version
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "read_secrets" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = concat(
      [for secret in aws_secretsmanager_secret.operator : secret.arn],
      [aws_secretsmanager_secret.grafana_password.arn],
    )
  }
}

resource "aws_iam_role" "this" {
  name               = "${var.name}-monitoring"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy" "read_secrets" {
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

resource "aws_iam_role_policy_attachment" "managed" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
  ])

  role       = aws_iam_role.this.name
  policy_arn = each.value
}

resource "aws_iam_instance_profile" "this" {
  name = "${var.name}-monitoring"
  role = aws_iam_role.this.name
}

# Metrics live on their own encrypted disk, so replacing the host does not lose them.
# EBS rather than EFS because Prometheus does not support NFS for its local storage.
resource "aws_ebs_volume" "data" {
  availability_zone = data.aws_subnet.this.availability_zone
  size              = var.data_volume_gib
  type              = "gp3"
  encrypted         = true

  tags = {
    Name = "${var.name}-monitoring-data"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_instance" "this" {
  ami                         = data.aws_ssm_parameter.ami.value
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = [aws_security_group.this.id]
  iam_instance_profile        = aws_iam_instance_profile.this.name
  associate_public_ip_address = false
  user_data_replace_on_change = true

  user_data = templatefile("${path.module}/bootstrap.sh.tftpl", {
    region             = var.region
    name               = var.name
    volume_id          = aws_ebs_volume.data.id
    prometheus_image   = var.prometheus_image
    alertmanager_image = var.alertmanager_image
    grafana_image      = var.grafana_image
  })

  # A hop limit of 1 keeps the instance's AWS role away from the containers.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    encrypted   = true
    volume_type = "gp3"
    volume_size = 20
  }

  tags = {
    Name = "${var.name}-monitoring"
  }

  lifecycle {
    ignore_changes = [ami]
  }
}

resource "aws_volume_attachment" "data" {
  device_name                    = "/dev/sdf"
  volume_id                      = aws_ebs_volume.data.id
  instance_id                    = aws_instance.this.id
  stop_instance_before_detaching = true
}

output "security_group_id" {
  value = aws_security_group.this.id
}

output "instance_id" {
  value = aws_instance.this.id
}

output "operator_secret_names" {
  value = [for secret in aws_secretsmanager_secret.operator : secret.name]
}
