variable "region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "eu-central-1"
}

variable "name" {
  description = "Prefix for every resource name."
  type        = string
  default     = "quote-api"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.name))
    error_message = "name must be lower case letters, digits and hyphens, at most 21 characters."
  }
}

variable "vpc_cidr" {
  description = "Address range of the VPC. Must be a /16."
  type        = string
  default     = "10.40.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0)) && endswith(var.vpc_cidr, "/16")
    error_message = "vpc_cidr must be a valid /16."
  }
}

variable "one_nat_gateway" {
  description = "Share one NAT gateway between zones. Cheaper, but a zone failure can cut outbound traffic."
  type        = bool
  default     = false
}

variable "certificate_arn" {
  description = "ACM certificate for the public HTTPS listener."
  type        = string

  validation {
    condition     = can(regex("^arn:aws:acm:[a-z0-9-]+:[0-9]{12}:certificate/.+$", var.certificate_arn))
    error_message = "certificate_arn must be an ACM certificate ARN."
  }
}

variable "stable_tag" {
  description = "Image tag the stable track runs. It is also the version label in the metrics."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,128}$", var.stable_tag)) && var.stable_tag != "latest"
    error_message = "stable_tag must be a real release tag, never latest."
  }
}

variable "canary_tag" {
  description = "Image tag the canary track runs. Null when no release is in progress."
  type        = string
  default     = null

  validation {
    condition     = var.canary_tag == null || (can(regex("^[A-Za-z0-9._-]{1,128}$", var.canary_tag)) && var.canary_tag != "latest")
    error_message = "canary_tag must be a real release tag, never latest."
  }
}

variable "monitoring_tag" {
  description = "Image tag of the Prometheus, Alertmanager and Grafana images."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,128}$", var.monitoring_tag)) && var.monitoring_tag != "latest"
    error_message = "monitoring_tag must be a real release tag, never latest."
  }
}

variable "stable_min_tasks" {
  description = "Fewest stable tasks to run. Two or more, so one zone can fail."
  type        = number
  default     = 2

  validation {
    condition     = var.stable_min_tasks >= 2
    error_message = "stable_min_tasks must be at least 2."
  }
}

variable "stable_max_tasks" {
  description = "Most stable tasks autoscaling may run."
  type        = number
  default     = 10
}

variable "canary_tasks" {
  description = "Canary tasks to run while a release is in progress."
  type        = number
  default     = 2

  validation {
    condition     = var.canary_tasks >= 1
    error_message = "canary_tasks must be at least 1."
  }
}

variable "grafana_password_version" {
  description = "Raise this by one to generate a new Grafana admin password."
  type        = number
  default     = 1
}

variable "alb_deletion_protection" {
  description = "Stops the load balancer being deleted by accident."
  type        = bool
  default     = true
}
