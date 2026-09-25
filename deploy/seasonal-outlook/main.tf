terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0, < 8.0"
    }
  }
}

# Seasonal Outlook storage, indexes and IAM. Since September 2026 the phases run in a
# background thread of the existing app service, so there is no Cloud Run Job.
#
# Upgrading a state that still has the Job: apply this file only after the new app
# revision has been accepted, because the previous revision still dispatches the Job
# and applying moves the Vertex grant from the worker to the app. Before deploying the
# new revision, grant the app identity roles/aiplatform.user by hand
# (CONSOLE_SETUP.md, section 8). The Job was created with deletion protection: either
# set `deletion_protection = false` on it and apply once before applying this file, or
# delete the Job in the console and run
# `terraform state rm google_cloud_run_v2_job.worker`. Rename `job_region` to `region`
# in terraform.tfvars and drop `job_name` and `image`.

provider "google" {
  project = var.project_id
  region  = var.region
}

variable "project_id" { type = string }
variable "region" { type = string }
variable "bucket_name" { type = string }
variable "worker_service_account_id" {
  type        = string
  description = "Account that signs download links (its historical name is the former Job worker's)."
}
variable "app_service_account_email" { type = string }
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
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  lifecycle { prevent_destroy = true }
}

locals {
  # A signed URL grants the access of the identity that signs it, so the signer reads the bucket.
  bucket_roles = {
    app    = { member = var.app_service_account_email, role = "roles/storage.objectUser" }
    signer = { member = google_service_account.worker.email, role = "roles/storage.objectViewer" }
  }
  environment = {
    SEASONAL_DRAFTER_ENABLED = tostring(var.enabled)
    SEASONAL_PROJECT         = var.project_id
    SEASONAL_BUCKET          = google_storage_bucket.artifacts.name
    SEASONAL_DATABASE        = var.firestore_database
    SEASONAL_COLLECTION      = "seasonal_outlook_runs"
    SEASONAL_PREFIX          = "seasonal-outlook"
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
  for_each = { app = var.app_service_account_email }
  project  = var.project_id
  role     = "roles/datastore.user"
  member   = "serviceAccount:${each.value}"
}

resource "google_storage_bucket_iam_member" "objects" {
  for_each = local.bucket_roles
  bucket   = google_storage_bucket.artifacts.name
  role     = each.value.role
  member   = "serviceAccount:${each.value.member}"
}

# The app service calls Gemini for the Seasonal phases.
resource "google_project_iam_member" "vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${var.app_service_account_email}"
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
