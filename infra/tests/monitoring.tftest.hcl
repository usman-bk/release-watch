mock_provider "aws" {
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

  mock_resource "aws_ebs_volume" {
    defaults = {
      id = "vol-0123456789abcdef0"
    }
  }

  mock_resource "aws_instance" {
    defaults = {
      id = "i-0123456789abcdef0"
    }
  }

  mock_resource "aws_secretsmanager_secret" {
    defaults = {
      arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:x"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/monitoring"
    }
  }
}

variables {
  name                     = "quote-api"
  region                   = "eu-central-1"
  vpc_id                   = "vpc-1"
  vpc_cidr                 = "10.40.0.0/16"
  subnet_id                = "subnet-c"
  prometheus_image         = "repo/prometheus:m1"
  alertmanager_image       = "repo/alertmanager:m1"
  grafana_image            = "repo/grafana:m1"
  grafana_password_version = 1
}

run "the_host_is_private_and_its_disks_are_encrypted" {
  command = apply

  module {
    source = "./modules/monitoring"
  }

  assert {
    condition     = aws_instance.this.associate_public_ip_address == false
    error_message = "the monitoring host must have no public address"
  }

  assert {
    condition     = one(aws_instance.this.metadata_options).http_tokens == "required"
    error_message = "IMDSv2 must be required"
  }

  assert {
    condition     = one(aws_instance.this.metadata_options).http_put_response_hop_limit == 1
    error_message = "containers must not reach the instance role"
  }

  assert {
    condition     = one(aws_instance.this.root_block_device).encrypted == true
    error_message = "the root disk must be encrypted"
  }

  assert {
    condition     = aws_ebs_volume.data.encrypted == true
    error_message = "the data disk must be encrypted"
  }

  assert {
    condition     = aws_ebs_volume.data.availability_zone == "eu-central-1a"
    error_message = "the data disk must be in the host's zone"
  }
}

run "nothing_can_connect_in_and_scrapes_stay_inside_the_vpc" {
  command = apply

  module {
    source = "./modules/monitoring"
  }

  assert {
    condition     = length(aws_security_group.this.ingress) == 0
    error_message = "the monitoring host should accept no inbound traffic"
  }

  assert {
    condition     = aws_vpc_security_group_egress_rule.scrape.cidr_ipv4 == "10.40.0.0/16" && aws_vpc_security_group_egress_rule.scrape.to_port == 8000
    error_message = "scrapes should only go to the service port inside the VPC"
  }
}

run "the_bootstrap_script_gets_every_value_and_keeps_a_month_of_metrics" {
  command = apply

  module {
    source = "./modules/monitoring"
  }

  assert {
    condition     = strcontains(aws_instance.this.user_data, "repo/prometheus:m1") && strcontains(aws_instance.this.user_data, "repo/grafana:m1") && strcontains(aws_instance.this.user_data, "repo/alertmanager:m1")
    error_message = "the host should run the images it was given"
  }

  assert {
    condition     = strcontains(aws_instance.this.user_data, "retention.time=35d")
    error_message = "retention must cover the 30 day error budget window"
  }

  assert {
    condition     = strcontains(aws_instance.this.user_data, "vol-0123456789abcdef0")
    error_message = "the host should look for its data disk by volume id"
  }

  assert {
    condition     = aws_instance.this.user_data_replace_on_change == true
    error_message = "a new monitoring image should replace the host"
  }
}

run "the_host_can_only_read_its_own_secrets" {
  command = plan

  module {
    source = "./modules/monitoring"
  }

  assert {
    condition     = one(data.aws_iam_policy_document.read_secrets.statement).actions == toset(["secretsmanager:GetSecretValue"])
    error_message = "the host should only be able to read secrets"
  }

  assert {
    condition     = length(aws_secretsmanager_secret.operator) == 3
    error_message = "expected the three alert webhook secrets"
  }
}

run "the_grafana_password_is_write_only" {
  command = apply

  module {
    source = "./modules/monitoring"
  }

  assert {
    condition     = aws_secretsmanager_secret_version.grafana_password.secret_string_wo_version == 1
    error_message = "the password should be written through the write-only argument"
  }

  assert {
    condition     = aws_secretsmanager_secret_version.grafana_password.secret_string == null || aws_secretsmanager_secret_version.grafana_password.secret_string == ""
    error_message = "the password must not be kept in state"
  }
}
