mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = {
      names = ["eu-central-1a", "eu-central-1b", "eu-central-1c"]
    }
  }

  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  mock_resource "aws_cloudwatch_log_group" {
    defaults = {
      arn = "arn:aws:logs:eu-central-1:123456789012:log-group:flow"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/flow"
    }
  }

  mock_resource "aws_vpc" {
    defaults = {
      cidr_block = "10.40.0.0/16"
    }
  }
}

variables {
  name            = "quote-api"
  cidr            = "10.40.0.0/16"
  one_nat_gateway = false
}

run "two_zones_of_public_and_private_subnets" {
  command = apply

  module {
    source = "./modules/network"
  }

  assert {
    condition     = length(aws_subnet.public) == 2 && length(aws_subnet.private) == 2
    error_message = "expected two public and two private subnets"
  }

  assert {
    condition     = toset([for s in aws_subnet.private : s.availability_zone]) == toset(["eu-central-1a", "eu-central-1b"])
    error_message = "private subnets must span two zones"
  }

  assert {
    condition     = alltrue([for s in aws_subnet.private : s.map_public_ip_on_launch == false])
    error_message = "private subnets must not hand out public addresses"
  }

  assert {
    condition     = length(setintersection([for s in aws_subnet.public : s.cidr_block], [for s in aws_subnet.private : s.cidr_block])) == 0
    error_message = "public and private ranges must not overlap"
  }

  assert {
    condition     = length(aws_nat_gateway.this) == 2
    error_message = "each zone gets its own NAT gateway by default"
  }

  assert {
    condition     = length(aws_default_security_group.this.ingress) == 0 && length(aws_default_security_group.this.egress) == 0
    error_message = "the default security group must stay empty"
  }

  assert {
    condition     = aws_flow_log.this.traffic_type == "ALL"
    error_message = "flow logs must record accepted and rejected traffic"
  }
}

run "private_routes_use_nat_and_public_routes_use_the_internet_gateway" {
  command = apply

  module {
    source = "./modules/network"
  }

  assert {
    condition     = alltrue([for t in aws_route_table.private : one(t.route).nat_gateway_id != "" && one(t.route).nat_gateway_id != null])
    error_message = "private subnets must leave through a NAT gateway"
  }

  assert {
    condition     = one(aws_route_table.public.route).gateway_id == aws_internet_gateway.this.id
    error_message = "public subnets must leave through the internet gateway"
  }
}

run "one_shared_nat_gateway_when_asked" {
  command = apply

  module {
    source = "./modules/network"
  }

  variables {
    one_nat_gateway = true
  }

  assert {
    condition     = length(aws_nat_gateway.this) == 1
    error_message = "one_nat_gateway should create exactly one"
  }

  assert {
    condition     = length(toset([for t in aws_route_table.private : one(t.route).nat_gateway_id])) == 1
    error_message = "both zones should route through the one gateway"
  }
}
