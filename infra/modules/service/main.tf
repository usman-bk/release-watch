terraform {
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.72"
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

variable "public_subnet_ids" {
  type = list(string)
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "certificate_arn" {
  type = string
}

variable "image_repository" {
  type = string
}

variable "stable_tag" {
  type = string
}

variable "canary_tag" {
  type = string
}

variable "stable_min_tasks" {
  type = number
}

variable "stable_max_tasks" {
  type = number
}

variable "canary_tasks" {
  type = number
}

variable "scraper_security_group_id" {
  type = string
}

variable "deletion_protection" {
  type = bool
}

locals {
  port = 8000

  tracks = {
    stable = {
      tag           = var.stable_tag
      desired_count = var.stable_min_tasks
    }
    # With no release in progress the canary service exists but runs nothing.
    canary = {
      tag           = coalesce(var.canary_tag, var.stable_tag)
      desired_count = var.canary_tag == null ? 0 : var.canary_tasks
    }
  }
}

resource "aws_security_group" "alb" {
  name_prefix = "${var.name}-alb-"
  description = "Public entry to the load balancer"
  vpc_id      = var.vpc_id

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "tasks" {
  name_prefix = "${var.name}-tasks-"
  description = "Service tasks"
  vpc_id      = var.vpc_id

  lifecycle {
    create_before_destroy = true
  }
}

#trivy:ignore:AVD-AWS-0107 This is the public entry point of the service.
resource "aws_vpc_security_group_ingress_rule" "alb_public" {
  for_each = toset(["80", "443"])

  security_group_id = aws_security_group.alb.id
  description       = "Public web traffic"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = tonumber(each.key)
  to_port           = tonumber(each.key)
}

resource "aws_vpc_security_group_egress_rule" "alb_to_tasks" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Forward to tasks"
  referenced_security_group_id = aws_security_group.tasks.id
  ip_protocol                  = "tcp"
  from_port                    = local.port
  to_port                      = local.port
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_alb" {
  security_group_id            = aws_security_group.tasks.id
  description                  = "Traffic from the load balancer"
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = local.port
  to_port                      = local.port
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_scraper" {
  security_group_id            = aws_security_group.tasks.id
  description                  = "Metrics scrapes from Prometheus"
  referenced_security_group_id = var.scraper_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = local.port
  to_port                      = local.port
}

#trivy:ignore:AVD-AWS-0104 Tasks pull images and write logs through the NAT gateway.
resource "aws_vpc_security_group_egress_rule" "tasks_https" {
  security_group_id = aws_security_group.tasks.id
  description       = "Pull images and write logs"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

#trivy:ignore:AVD-AWS-0053 The service is public, so the load balancer has to be.
resource "aws_lb" "this" {
  name                       = var.name
  load_balancer_type         = "application"
  internal                   = false
  subnets                    = var.public_subnet_ids
  security_groups            = [aws_security_group.alb.id]
  drop_invalid_header_fields = true
  enable_deletion_protection = var.deletion_protection
}

resource "aws_lb_target_group" "this" {
  for_each = local.tracks

  name                 = "${var.name}-${each.key}"
  vpc_id               = var.vpc_id
  port                 = local.port
  protocol             = "HTTP"
  target_type          = "ip"
  deregistration_delay = 30

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 10
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type = "forward"

    forward {
      target_group {
        arn    = aws_lb_target_group.this["stable"].arn
        weight = 100
      }

      target_group {
        arn    = aws_lb_target_group.this["canary"].arn
        weight = 0
      }

      stickiness {
        enabled  = false
        duration = 1
      }
    }
  }

  # The release gate owns the weights. Without this, the next apply in the middle
  # of a release would silently send all traffic back to stable.
  lifecycle {
    ignore_changes = [default_action]
  }
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_ecs_cluster" "this" {
  name = var.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_service_discovery_private_dns_namespace" "this" {
  name = "${var.name}.internal"
  vpc  = var.vpc_id
}

resource "aws_service_discovery_service" "this" {
  for_each = local.tracks
  name     = each.key

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.this.id

    dns_records {
      type = "A"
      ttl  = 10
    }

    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {}
}

resource "aws_cloudwatch_log_group" "this" {
  for_each          = local.tracks
  name              = "/${var.name}/${each.key}"
  retention_in_days = 30
}

data "aws_iam_policy_document" "tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# No task role: the service calls no AWS API, so it is given no AWS identity.
resource "aws_ecs_task_definition" "this" {
  for_each = local.tracks

  family                   = "${var.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([{
    name                   = "app"
    image                  = "${var.image_repository}:${each.value.tag}"
    essential              = true
    privileged             = false
    readonlyRootFilesystem = true
    user                   = "10001"

    portMappings = [{
      containerPort = local.port
      protocol      = "tcp"
    }]

    environment = [{
      name  = "APP_VERSION"
      value = each.value.tag
    }]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.this[each.key].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "app"
      }
    }
  }])
}

resource "aws_ecs_service" "stable" {
  name                               = "stable"
  cluster                            = aws_ecs_cluster.this.id
  task_definition                    = aws_ecs_task_definition.this["stable"].arn
  desired_count                      = local.tracks["stable"].desired_count
  launch_type                        = "FARGATE"
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 30
  wait_for_steady_state              = true

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.this["stable"].arn
    container_name   = "app"
    container_port   = local.port
  }

  service_registries {
    registry_arn = aws_service_discovery_service.this["stable"].arn
  }

  depends_on = [aws_lb_listener.https, aws_iam_role_policy_attachment.execution]

  # Autoscaling owns this number. Without this, every release would shrink a
  # scaled-up service back to its minimum.
  lifecycle {
    ignore_changes = [desired_count]
  }
}

resource "aws_ecs_service" "canary" {
  name                               = "canary"
  cluster                            = aws_ecs_cluster.this.id
  task_definition                    = aws_ecs_task_definition.this["canary"].arn
  desired_count                      = local.tracks["canary"].desired_count
  launch_type                        = "FARGATE"
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 30
  wait_for_steady_state              = true

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.this["canary"].arn
    container_name   = "app"
    container_port   = local.port
  }

  service_registries {
    registry_arn = aws_service_discovery_service.this["canary"].arn
  }

  depends_on = [aws_lb_listener.https, aws_iam_role_policy_attachment.execution]
}

resource "aws_appautoscaling_target" "stable" {
  service_namespace  = "ecs"
  scalable_dimension = "ecs:service:DesiredCount"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.stable.name}"
  min_capacity       = var.stable_min_tasks
  max_capacity       = var.stable_max_tasks
}

resource "aws_appautoscaling_policy" "stable_requests" {
  name               = "${var.name}-stable-requests"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.stable.service_namespace
  scalable_dimension = aws_appautoscaling_target.stable.scalable_dimension
  resource_id        = aws_appautoscaling_target.stable.resource_id

  target_tracking_scaling_policy_configuration {
    target_value = 1000

    predefined_metric_specification {
      predefined_metric_type = "ALBRequestCountPerTarget"
      resource_label         = "${aws_lb.this.arn_suffix}/${aws_lb_target_group.this["stable"].arn_suffix}"
    }
  }
}

data "aws_iam_policy_document" "release_gate" {
  statement {
    actions   = ["elasticloadbalancing:ModifyListener"]
    resources = [aws_lb_listener.https.arn]
  }
}

resource "aws_iam_policy" "release_gate" {
  name        = "${var.name}-release-gate"
  description = "Lets the release gate change the traffic weights on the one listener"
  policy      = data.aws_iam_policy_document.release_gate.json
}

output "load_balancer_dns_name" {
  value = aws_lb.this.dns_name
}

output "https_listener_arn" {
  value = aws_lb_listener.https.arn
}

output "target_group_arns" {
  value = { for track, group in aws_lb_target_group.this : track => group.arn }
}

output "release_gate_policy_arn" {
  value = aws_iam_policy.release_gate.arn
}
