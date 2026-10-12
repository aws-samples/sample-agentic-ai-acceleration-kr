# Partial backend config: the bucket name embeds the account id, which does not
# belong in the repository (a hard-coded bucket would have to be edited before
# every `init` and is easy to commit by accident), so it is passed in instead:
#
#   terraform init -backend-config=backend.hcl
#
# See backend.hcl.example for the file to copy. The bucket and lock table come
# from `infra/bootstrap` applied with `-var project=bap`.
terraform {
  backend "s3" {
    key     = "standalone/terraform.tfstate"
    region  = "ap-northeast-1"
    encrypt = true
  }
}
