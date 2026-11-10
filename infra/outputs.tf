output "public_dns_name" {
  description = "Point the service's DNS record here."
  value       = module.service.load_balancer_dns_name
}

output "listener_arn" {
  description = "Passed to the release gate as --listener-arn."
  value       = module.service.https_listener_arn
}

output "stable_group_arn" {
  description = "Passed to the release gate as --stable-group-arn."
  value       = module.service.target_group_arns["stable"]
}

output "canary_group_arn" {
  description = "Passed to the release gate as --canary-group-arn."
  value       = module.service.target_group_arns["canary"]
}

output "release_gate_policy_arn" {
  description = "Attach to whatever runs the release gate."
  value       = module.service.release_gate_policy_arn
}

output "monitoring_instance_id" {
  description = "Target for an SSM port forward to Prometheus and Grafana."
  value       = module.monitoring.instance_id
}

output "monitoring_secret_names" {
  description = "Secrets to fill in before the alert routes can deliver."
  value       = module.monitoring.operator_secret_names
}
