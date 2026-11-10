provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.name
      ManagedBy = "terraform"
    }
  }
}

module "network" {
  source          = "./modules/network"
  name            = var.name
  cidr            = var.vpc_cidr
  one_nat_gateway = var.one_nat_gateway
}

resource "aws_ecr_repository" "this" {
  for_each = toset(["app", "prometheus", "alertmanager", "grafana"])

  name                 = "${var.name}/${each.key}"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each   = aws_ecr_repository.this
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 30 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 30
      }
      action = { type = "expire" }
    }]
  })
}

module "monitoring" {
  source = "./modules/monitoring"

  name                     = var.name
  region                   = var.region
  vpc_id                   = module.network.vpc_id
  vpc_cidr                 = module.network.vpc_cidr
  subnet_id                = module.network.private_subnet_ids[0]
  prometheus_image         = "${aws_ecr_repository.this["prometheus"].repository_url}:${var.monitoring_tag}"
  alertmanager_image       = "${aws_ecr_repository.this["alertmanager"].repository_url}:${var.monitoring_tag}"
  grafana_image            = "${aws_ecr_repository.this["grafana"].repository_url}:${var.monitoring_tag}"
  grafana_password_version = var.grafana_password_version
}

module "service" {
  source = "./modules/service"

  name                      = var.name
  region                    = var.region
  vpc_id                    = module.network.vpc_id
  public_subnet_ids         = module.network.public_subnet_ids
  private_subnet_ids        = module.network.private_subnet_ids
  certificate_arn           = var.certificate_arn
  image_repository          = aws_ecr_repository.this["app"].repository_url
  stable_tag                = var.stable_tag
  canary_tag                = var.canary_tag
  stable_min_tasks          = var.stable_min_tasks
  stable_max_tasks          = var.stable_max_tasks
  canary_tasks              = var.canary_tasks
  scraper_security_group_id = module.monitoring.security_group_id
  deletion_protection       = var.alb_deletion_protection
}
