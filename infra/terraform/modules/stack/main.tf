# Composition of the core RAG stack for one environment. LangSmith is NOT part of this stack.

terraform {
  required_providers {
    aws = {
      source                = "hashicorp/aws"
      configuration_aliases = [aws.us_east_1]
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  name       = "${var.project}-${var.environment}"
  bedrock_resource_arns = concat(
    [for m in var.bedrock_in_region_models : "arn:aws:bedrock:${var.region}::foundation-model/${m}"],
    [for p in var.bedrock_inference_profiles : "arn:aws:bedrock:${var.region}:${local.account_id}:inference-profile/${p}"],
    flatten([for m in var.bedrock_geo_models : [for r in var.bedrock_geo_regions : "arn:aws:bedrock:${r}::foundation-model/${m}"]]),
  )
}

module "kms" {
  source     = "../kms"
  name       = local.name
  region     = var.region
  account_id = local.account_id
}

module "network" {
  source                     = "../network"
  name                       = local.name
  region                     = var.region
  account_id                 = local.account_id
  cidr_block                 = var.vpc_cidr
  az_count                   = var.az_count
  single_nat_gateway         = var.single_nat_gateway
  enable_network_firewall    = var.enable_network_firewall
  enable_interface_endpoints = var.enable_interface_endpoints
  deletion_protection        = var.deletion_protection
  logs_kms_key_arn           = module.kms.logs_key_arn
}

module "storage" {
  source                    = "../storage"
  name                      = local.name
  region                    = var.region
  account_id                = local.account_id
  kms_key_arn               = module.kms.data_key_arn
  force_destroy             = !var.deletion_protection
  enable_malware_protection = var.enable_malware_protection
}

module "dynamodb" {
  source              = "../dynamodb"
  prefix              = "${local.name}-"
  kms_key_arn         = module.kms.data_key_arn
  deletion_protection = var.deletion_protection
}

module "ecr" {
  source       = "../ecr"
  name         = local.name
  kms_key_arn  = module.kms.data_key_arn
  force_delete = !var.deletion_protection
}

module "secrets" {
  source      = "../secrets"
  name        = local.name
  kms_key_arn = module.kms.data_key_arn
}

module "edge" {
  source = "../edge"
  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }
  name                       = local.name
  account_id                 = local.account_id
  vpc_id                     = module.network.vpc_id
  vpc_cidr                   = module.network.vpc_cidr
  private_subnet_ids         = module.network.app_subnet_ids
  alb_certificate_arn        = var.alb_certificate_arn
  cloudfront_certificate_arn = var.cloudfront_certificate_arn
  domain_names               = var.domain_names
  deletion_protection        = var.deletion_protection
  force_destroy              = !var.deletion_protection
}

module "ingestion" {
  source           = "../ingestion"
  name             = local.name
  account_id       = local.account_id
  source_bucket    = module.storage.source_bucket
  kms_key_arn      = module.kms.data_key_arn
  logs_kms_key_arn = module.kms.logs_key_arn
}

module "observability" {
  source                 = "../observability"
  name                   = local.name
  region                 = var.region
  account_id             = local.account_id
  audit_kms_key_arn      = module.kms.audit_key_arn
  logs_kms_key_arn       = module.kms.logs_key_arn
  data_kms_key_arn       = module.kms.data_key_arn
  data_event_bucket_arns = [module.storage.source_bucket_arn, module.storage.artifacts_bucket_arn]
  dlq_names              = module.ingestion.dlq_names
  state_machine_arn      = module.ingestion.state_machine_arn
  alb_arn_suffix         = module.edge.alb_arn_suffix
  opensearch_domain_name = "${local.name}-search"
  alarm_emails           = var.alarm_emails
  force_destroy          = !var.deletion_protection
}

module "ecs" {
  source                = "../ecs"
  name                  = local.name
  environment           = var.environment
  region                = var.region
  account_id            = local.account_id
  image                 = "${module.ecr.repository_urls["app"]}:${var.image_tag}"
  vpc_id                = module.network.vpc_id
  app_subnet_ids        = module.network.app_subnet_ids
  alb_security_group_id = module.edge.alb_security_group_id
  target_group_arn      = module.edge.target_group_arn
  # The API refuses to start without an Entra app registration, so keep it at 0 tasks until one is configured.
  api_desired_count     = var.entra_api_client_id == "" ? 0 : var.api_desired_count
  api_max_count         = var.api_max_count
  worker_desired_count  = var.worker_desired_count
  dynamodb_table_arns   = module.dynamodb.table_arns
  table_prefix          = "${local.name}-"
  opensearch_domain_arn = module.opensearch.domain_arn
  opensearch_endpoint   = module.opensearch.endpoint
  bedrock_resource_arns = local.bedrock_resource_arns
  data_kms_key_arn      = module.kms.data_key_arn
  logs_kms_key_arn      = module.kms.logs_key_arn
  audit_kms_key_arn     = module.kms.audit_key_arn
  audit_log_group_arn   = module.observability.audit_log_group_arn
  audit_log_group_name  = module.observability.audit_log_group_name
  # Secrets are read at runtime by ARN (ERP_GRAPH_CLIENT_SECRET_ARN) only when needed, never injected as env
  # vars — so an unset secret (e.g. before Entra/Graph exists) cannot block task startup.
  secret_arns          = {}
  runtime_secret_arns  = values(module.secrets.arns)
  artifacts_bucket     = module.storage.artifacts_bucket
  artifacts_bucket_arn = module.storage.artifacts_bucket_arn
  source_bucket        = module.storage.source_bucket
  source_bucket_arn    = module.storage.source_bucket_arn
  worker_queue_arn     = module.ingestion.worker_queue_arn
  worker_queue_url     = module.ingestion.worker_queue_url
  state_machine_arn    = module.ingestion.state_machine_arn
  cors_origins         = concat(["https://${module.edge.cloudfront_domain}"], [for d in var.domain_names : "https://${d}"])
  extra_environment = {
    ERP_ENTRA_API_CLIENT_ID     = var.entra_api_client_id
    ERP_ENTRA_ALLOWED_TENANTS   = jsonencode(var.entra_allowed_tenants)
    ERP_API_AUDIENCE            = var.entra_api_client_id
    ERP_GRAPH_CLIENT_ID         = var.graph_client_id
    ERP_GRAPH_CLIENT_SECRET_ARN = module.secrets.arns["graph-client-secret"]
    ERP_MODEL_REGISTRY_PATH     = "/app/config/models.bedrock.yaml"
  }
}

module "opensearch" {
  source                    = "../opensearch"
  name                      = local.name
  account_id                = local.account_id
  domain_name               = "${local.name}-search"
  instance_type             = var.opensearch_instance_type
  instance_count            = var.opensearch_instance_count
  dedicated_master_count    = var.opensearch_dedicated_master_count
  volume_size_gb            = var.opensearch_volume_gb
  vpc_id                    = module.network.vpc_id
  subnet_ids                = module.network.data_subnet_ids
  client_security_group_ids = { ecs_tasks = module.ecs.tasks_security_group_id }
  client_role_arns          = module.ecs.task_role_arns
  master_role_arn           = var.opensearch_master_role_arn != "" ? var.opensearch_master_role_arn : module.ecs.worker_role_arn
  kms_key_arn               = module.kms.data_key_arn
  logs_kms_key_arn          = module.kms.logs_key_arn
}

module "backup" {
  source        = "../backup"
  name          = local.name
  kms_key_arn   = module.kms.data_key_arn
  resource_arns = concat(module.dynamodb.table_arns, [module.storage.artifacts_bucket_arn])
  force_destroy = !var.deletion_protection
}

module "github_oidc" {
  count                       = var.github_repository == "" ? 0 : 1
  source                      = "../github_oidc"
  name                        = local.name
  region                      = var.region
  account_id                  = local.account_id
  create_provider             = var.create_github_oidc_provider
  github_repository           = var.github_repository
  github_environment          = var.environment
  ecr_repository_arns         = module.ecr.repository_arns
  task_role_arns              = concat(module.ecs.task_role_arns, [module.ecs.execution_role_arn])
  web_bucket_arn              = module.edge.web_bucket_arn
  cloudfront_distribution_arn = module.edge.cloudfront_distribution_arn
}
