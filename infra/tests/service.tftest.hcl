mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/execution"
    }
  }

  mock_resource "aws_lb" {
    defaults = {
      arn = "arn:aws:elasticloadbalancing:eu-central-1:123456789012:loadbalancer/app/a/b"
    }
  }

  mock_resource "aws_lb_target_group" {
    defaults = {
      arn = "arn:aws:elasticloadbalancing:eu-central-1:123456789012:targetgroup/a/b"
    }
  }

  mock_resource "aws_lb_listener" {
    defaults = {
      arn = "arn:aws:elasticloadbalancing:eu-central-1:123456789012:listener/app/a/b/c"
    }
  }

  mock_resource "aws_service_discovery_service" {
    defaults = {
      arn = "arn:aws:servicediscovery:eu-central-1:123456789012:service/srv-1"
    }
  }

  mock_resource "aws_ecs_task_definition" {
    defaults = {
      arn = "arn:aws:ecs:eu-central-1:123456789012:task-definition/quote-api:1"
    }
  }

  mock_resource "aws_cloudwatch_log_group" {
    defaults = {
      arn = "arn:aws:logs:eu-central-1:123456789012:log-group:g"
    }
  }
}

override_resource {
  target = aws_lb_target_group.this["stable"]
  values = {
    arn = "arn:aws:elasticloadbalancing:eu-central-1:123456789012:targetgroup/stable/1"
  }
}

override_resource {
  target = aws_lb_target_group.this["canary"]
  values = {
    arn = "arn:aws:elasticloadbalancing:eu-central-1:123456789012:targetgroup/canary/2"
  }
}

variables {
  name                      = "quote-api"
  region                    = "eu-central-1"
  vpc_id                    = "vpc-1"
  public_subnet_ids         = ["subnet-a", "subnet-b"]
  private_subnet_ids        = ["subnet-c", "subnet-d"]
  certificate_arn           = "arn:aws:acm:eu-central-1:123456789012:certificate/abc"
  image_repository          = "123456789012.dkr.ecr.eu-central-1.amazonaws.com/quote-api/app"
  stable_tag                = "v1"
  canary_tag                = null
  stable_min_tasks          = 2
  stable_max_tasks          = 10
  canary_tasks              = 2
  scraper_security_group_id = "sg-scraper"
  deletion_protection       = true
}

run "between_releases_the_canary_runs_nothing_and_gets_no_traffic" {
  command = apply

  module {
    source = "./modules/service"
  }

  assert {
    condition     = aws_ecs_service.canary.desired_count == 0
    error_message = "with no release in progress the canary should run no tasks"
  }

  assert {
    condition     = aws_ecs_service.stable.desired_count == 2
    error_message = "stable should start at its minimum"
  }

  assert {
    condition     = one([for g in one(one(aws_lb_listener.https.default_action).forward).target_group : g.weight if g.arn == aws_lb_target_group.this["stable"].arn]) == 100
    error_message = "stable should start with all the traffic"
  }

  assert {
    condition     = one([for g in one(one(aws_lb_listener.https.default_action).forward).target_group : g.weight if g.arn == aws_lb_target_group.this["canary"].arn]) == 0
    error_message = "the canary should start with no traffic"
  }

  assert {
    condition     = jsondecode(aws_ecs_task_definition.this["canary"].container_definitions)[0].image == "123456789012.dkr.ecr.eu-central-1.amazonaws.com/quote-api/app:v1"
    error_message = "an idle canary should reference the stable image, not a missing one"
  }
}

run "a_release_starts_canary_tasks_on_the_new_image_and_labels_them" {
  command = apply

  module {
    source = "./modules/service"
  }

  variables {
    canary_tag = "v2"
  }

  assert {
    condition     = aws_ecs_service.canary.desired_count == 2
    error_message = "a release should start the canary tasks"
  }

  assert {
    condition     = jsondecode(aws_ecs_task_definition.this["canary"].container_definitions)[0].image == "123456789012.dkr.ecr.eu-central-1.amazonaws.com/quote-api/app:v2"
    error_message = "the canary should run the new image"
  }

  assert {
    condition     = jsondecode(aws_ecs_task_definition.this["stable"].container_definitions)[0].image == "123456789012.dkr.ecr.eu-central-1.amazonaws.com/quote-api/app:v1"
    error_message = "stable must keep the old image during a release"
  }

  assert {
    condition     = jsondecode(aws_ecs_task_definition.this["canary"].container_definitions)[0].environment[0].value == "v2"
    error_message = "the version label the gate selects on must be the image tag"
  }
}

run "tasks_are_private_and_locked_down" {
  command = apply

  module {
    source = "./modules/service"
  }

  assert {
    condition     = alltrue([for s in [aws_ecs_service.stable, aws_ecs_service.canary] : one(s.network_configuration).assign_public_ip == false])
    error_message = "tasks must not have public addresses"
  }

  assert {
    condition     = alltrue([for s in [aws_ecs_service.stable, aws_ecs_service.canary] : one(s.network_configuration).subnets == toset(["subnet-c", "subnet-d"])])
    error_message = "tasks belong in the private subnets"
  }

  assert {
    condition     = aws_ecs_task_definition.this["stable"].task_role_arn == null || aws_ecs_task_definition.this["stable"].task_role_arn == ""
    error_message = "the service calls no AWS API, so it needs no task role"
  }

  assert {
    condition     = jsondecode(aws_ecs_task_definition.this["stable"].container_definitions)[0].readonlyRootFilesystem == true
    error_message = "the root filesystem should be read only"
  }

  assert {
    condition     = jsondecode(aws_ecs_task_definition.this["stable"].container_definitions)[0].privileged == false
    error_message = "containers must not be privileged"
  }

  assert {
    condition     = jsondecode(aws_ecs_task_definition.this["stable"].container_definitions)[0].user == "10001"
    error_message = "containers must not run as root"
  }
}

run "only_the_load_balancer_and_prometheus_can_reach_the_tasks" {
  command = apply

  module {
    source = "./modules/service"
  }

  assert {
    condition     = aws_vpc_security_group_ingress_rule.tasks_from_alb.referenced_security_group_id == aws_security_group.alb.id
    error_message = "tasks should accept traffic from the load balancer's group"
  }

  assert {
    condition     = aws_vpc_security_group_ingress_rule.tasks_from_scraper.referenced_security_group_id == "sg-scraper"
    error_message = "tasks should accept scrapes from the monitoring group"
  }

  assert {
    condition     = aws_vpc_security_group_ingress_rule.tasks_from_alb.cidr_ipv4 == null && aws_vpc_security_group_ingress_rule.tasks_from_scraper.cidr_ipv4 == null
    error_message = "task ingress must not be open to an address range"
  }

  assert {
    condition     = toset([for r in aws_vpc_security_group_ingress_rule.alb_public : r.to_port]) == toset([80, 443])
    error_message = "the load balancer should only be open on 80 and 443"
  }
}

run "the_public_edge_is_https_only" {
  command = apply

  module {
    source = "./modules/service"
  }

  assert {
    condition     = one(one(aws_lb_listener.http_redirect.default_action).redirect).status_code == "HTTP_301" && one(one(aws_lb_listener.http_redirect.default_action).redirect).protocol == "HTTPS"
    error_message = "port 80 should only redirect to HTTPS"
  }

  assert {
    condition     = can(regex("TLS13", aws_lb_listener.https.ssl_policy))
    error_message = "the listener should use a TLS 1.3 policy"
  }

  assert {
    condition     = aws_lb.this.drop_invalid_header_fields == true
    error_message = "invalid headers should be dropped"
  }

  assert {
    condition     = aws_lb.this.enable_deletion_protection == true
    error_message = "the load balancer should be protected from deletion"
  }
}

run "a_failed_deployment_stops_itself" {
  command = apply

  module {
    source = "./modules/service"
  }

  assert {
    condition     = alltrue([for s in [aws_ecs_service.stable, aws_ecs_service.canary] : one(s.deployment_circuit_breaker).enable && one(s.deployment_circuit_breaker).rollback])
    error_message = "the circuit breaker should stop and roll back a failing deployment"
  }

  assert {
    condition     = alltrue([for s in [aws_ecs_service.stable, aws_ecs_service.canary] : s.wait_for_steady_state])
    error_message = "apply should wait, so a failed canary fails the pipeline"
  }

  assert {
    condition     = alltrue([for g in aws_lb_target_group.this : one(g.health_check).path == "/healthz" && one(g.health_check).matcher == "200"])
    error_message = "health checks should use the health endpoint"
  }
}

run "discovery_names_match_the_prometheus_config" {
  command = apply

  module {
    source = "./modules/service"
  }

  assert {
    condition     = aws_service_discovery_private_dns_namespace.this.name == "quote-api.internal"
    error_message = "prometheus.yml looks in quote-api.internal"
  }

  assert {
    condition     = toset([for s in aws_service_discovery_service.this : s.name]) == toset(["stable", "canary"])
    error_message = "prometheus.yml expects a stable and a canary name"
  }

  assert {
    condition     = alltrue([for s in aws_service_discovery_service.this : one(one(s.dns_config).dns_records).type == "A"])
    error_message = "prometheus.yml queries A records"
  }
}

run "autoscaling_stays_within_the_limits" {
  command = apply

  module {
    source = "./modules/service"
  }

  assert {
    condition     = aws_appautoscaling_target.stable.min_capacity == 2 && aws_appautoscaling_target.stable.max_capacity == 10
    error_message = "autoscaling should use the configured limits"
  }

  assert {
    condition     = aws_appautoscaling_target.stable.resource_id == "service/quote-api/stable"
    error_message = "autoscaling should target the stable service"
  }
}

run "logs_expire" {
  command = apply

  module {
    source = "./modules/service"
  }

  assert {
    condition     = alltrue([for g in aws_cloudwatch_log_group.this : g.retention_in_days == 30])
    error_message = "logs should expire after 30 days"
  }
}
