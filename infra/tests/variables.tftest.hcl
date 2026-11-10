mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = {
      names = ["eu-central-1a", "eu-central-1b"]
    }
  }

  mock_data "aws_ssm_parameter" {
    defaults = {
      value = "ami-0123456789abcdef0"
    }
  }

  mock_data "aws_subnet" {
    defaults = {
      availability_zone = "eu-central-1a"
    }
  }

  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  mock_resource "aws_cloudwatch_log_group" {
    defaults = {
      arn = "arn:aws:logs:eu-central-1:123456789012:log-group:g"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/r"
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

  mock_resource "aws_secretsmanager_secret" {
    defaults = {
      arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:x"
    }
  }

  mock_resource "aws_ecr_repository" {
    defaults = {
      repository_url = "123456789012.dkr.ecr.eu-central-1.amazonaws.com/quote-api/x"
    }
  }

  mock_resource "aws_vpc" {
    defaults = {
      cidr_block = "10.40.0.0/16"
    }
  }

  mock_resource "aws_ebs_volume" {
    defaults = {
      id = "vol-0123456789abcdef0"
    }
  }
}

variables {
  certificate_arn = "arn:aws:acm:eu-central-1:123456789012:certificate/abc"
  stable_tag      = "v1"
  monitoring_tag  = "m1"
}

run "the_whole_stack_plans_together" {
  command = apply

  assert {
    condition     = length(aws_ecr_repository.this) == 4
    error_message = "expected a repository for the app and each monitoring image"
  }

  assert {
    condition     = alltrue([for r in aws_ecr_repository.this : r.image_tag_mutability == "IMMUTABLE"])
    error_message = "tags must be immutable, because the version label depends on them"
  }

  assert {
    condition     = alltrue([for r in aws_ecr_repository.this : one(r.image_scanning_configuration).scan_on_push])
    error_message = "images should be scanned on push"
  }
}

run "latest_is_refused_for_stable" {
  command = plan

  variables {
    stable_tag = "latest"
  }

  expect_failures = [var.stable_tag]
}

run "latest_is_refused_for_canary" {
  command = plan

  variables {
    canary_tag = "latest"
  }

  expect_failures = [var.canary_tag]
}

run "latest_is_refused_for_monitoring" {
  command = plan

  variables {
    monitoring_tag = "latest"
  }

  expect_failures = [var.monitoring_tag]
}

run "a_tag_that_could_change_a_query_is_refused" {
  command = plan

  variables {
    stable_tag = "v1\"} or up{"
  }

  expect_failures = [var.stable_tag]
}

run "one_stable_task_is_refused" {
  command = plan

  variables {
    stable_min_tasks = 1
  }

  expect_failures = [var.stable_min_tasks]
}

run "no_canary_tasks_is_refused" {
  command = plan

  variables {
    canary_tasks = 0
  }

  expect_failures = [var.canary_tasks]
}

run "a_certificate_that_is_not_an_arn_is_refused" {
  command = plan

  variables {
    certificate_arn = "my-cert"
  }

  expect_failures = [var.certificate_arn]
}

run "a_vpc_that_is_not_a_16_is_refused" {
  command = plan

  variables {
    vpc_cidr = "10.0.0.0/24"
  }

  expect_failures = [var.vpc_cidr]
}

run "a_name_too_long_for_a_target_group_is_refused" {
  command = plan

  variables {
    name = "a-very-long-service-name-indeed"
  }

  expect_failures = [var.name]
}
