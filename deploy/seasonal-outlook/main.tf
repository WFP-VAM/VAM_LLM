terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0, < 8.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.job_region
}

variable "project_id" { type = string }
variable "job_region" { type = string }
variable "bucket_name" { type = string }
variable "worker_service_account_id" { type = string }
variable "app_service_account_email" { type = string }
variable "job_name" { type = string }
variable "image" { type = string }
variable "firestore_database" {
  type        = string
  description = "Existing company Firestore Native database; this module does not replace it."
}
variable "enabled" {
  type    = bool
  default = true
}

resource "google_project_service" "apis" {
  for_each           = toset(["run.googleapis.com", "firestore.googleapis.com", "storage.googleapis.com", "aiplatform.googleapis.com", "iamcredentials.googleapis.com"])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "worker" {
  account_id   = var.worker_service_account_id
  display_name = "Seasonal Outlook worker"
}

resource "google_storage_bucket" "artifacts" {
  name                        = var.bucket_name
  location                    = var.job_region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  lifecycle { prevent_destroy = true }
}

locals {
  identities = {
    app    = var.app_service_account_email
    worker = google_service_account.worker.email
  }
  environment = {
    SEASONAL_DRAFTER_ENABLED = tostring(var.enabled)
    SEASONAL_PROJECT         = var.project_id
    SEASONAL_BUCKET          = google_storage_bucket.artifacts.name
    SEASONAL_DATABASE        = var.firestore_database
    SEASONAL_COLLECTION      = "seasonal_outlook_runs"
    SEASONAL_PREFIX          = "seasonal-outlook"
    SEASONAL_JOB             = var.job_name
    SEASONAL_JOB_REGION      = var.job_region
    SEASONAL_SIGNER          = google_service_account.worker.email
    SEASONAL_MODEL           = "gemini-3.1-pro-preview"
    SEASONAL_LOCATION        = "global"
  }
  history_indexes = {
    region             = ["region_id"]
    date               = ["report_date"]
    status             = ["status"]
    region_date        = ["region_id", "report_date"]
    region_status      = ["region_id", "status"]
    date_status        = ["report_date", "status"]
    region_date_status = ["region_id", "report_date", "status"]
  }
}

resource "google_project_iam_member" "firestore" {
  for_each = local.identities
  project  = var.project_id
  role     = "roles/datastore.user"
  member   = "serviceAccount:${each.value}"
}

resource "google_storage_bucket_iam_member" "objects" {
  for_each = local.identities
  bucket   = google_storage_bucket.artifacts.name
  role     = "roles/storage.objectUser"
  member   = "serviceAccount:${each.value}"
}

resource "google_project_iam_member" "vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_custom_role" "dispatch" {
  role_id     = "seasonalJobDispatcher"
  title       = "Execute Seasonal job with run identifiers"
  permissions = ["run.jobs.run", "run.jobs.runWithOverrides"]
}

resource "google_project_iam_custom_role" "sign" {
  role_id     = "seasonalArtifactSigner"
  title       = "Sign short lived Seasonal download URLs"
  permissions = ["iam.serviceAccounts.signBlob"]
}

resource "google_service_account_iam_member" "sign" {
  service_account_id = google_service_account.worker.name
  role               = google_project_iam_custom_role.sign.name
  member             = "serviceAccount:${var.app_service_account_email}"
}

resource "google_cloud_run_v2_job" "worker" {
  name                = var.job_name
  location            = var.job_region
  deletion_protection = true
  depends_on          = [google_project_service.apis]
  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = google_service_account.worker.email
      timeout         = "7200s"
      max_retries     = 0
      containers {
        image   = var.image
        command = ["python"]
        args    = ["-m", "app.services.seasonal_outlook.worker", "--help"]
        resources { limits = { cpu = "2", memory = "4Gi" } }
        dynamic "env" {
          for_each = local.environment
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
}

resource "google_cloud_run_v2_job_iam_member" "dispatch" {
  name     = google_cloud_run_v2_job.worker.name
  location = var.job_region
  role     = google_project_iam_custom_role.dispatch.name
  member   = "serviceAccount:${var.app_service_account_email}"
}

resource "google_firestore_index" "history" {
  for_each   = local.history_indexes
  project    = var.project_id
  database   = var.firestore_database
  collection = "seasonal_outlook_runs"
  dynamic "fields" {
    for_each = each.value
    content {
      field_path = fields.value
      order      = "ASCENDING"
    }
  }
  fields {
    field_path = "created_at"
    order      = "DESCENDING"
  }
}

# Only the history summary fields need indexes. Nested audit inventories can
# otherwise approach Firestore's index-entry limit long before the document limit.
resource "google_firestore_field" "audit_exemptions" {
  for_each   = toset(["operations", "requests", "versions", "maps", "artifacts", "input_artifact", "confirmation", "notes"])
  project    = var.project_id
  database   = var.firestore_database
  collection = "seasonal_outlook_runs"
  field      = each.value
  index_config {}
}

output "app_environment" {
  description = "Add these entries to the existing app service without replacing its other environment variables."
  value       = local.environment
}
