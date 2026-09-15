# `env/aws` — external S3 + RDS overlay

Points Kubeflow Pipelines at external AWS storage instead of the in-cluster
SeaweedFS + MySQL backends that ship in `env/platform-agnostic-multi-user`
(those resource lines are commented out there):

- **Artifacts → S3 via IRSA.** `aws-configuration-kfp-launcher-patch.yaml` sets
  the `kfp-launcher` provider to `credentials.fromEnv: true`; the
  `ml-pipeline`/`ml-pipeline-ui` patches null out the MinIO/object-store access
  keys (`value: ""`, `valueFrom: null`) so the AWS SDK uses the pod's IRSA role
  from the default credential chain. No static access/secret keys anywhere.
- **Argo archive + KFP databases → RDS MySQL.** `config` (the
  workflow-controller config) sets the S3 artifact repository and the
  `argo_archive` MySQL persistence; `params.env` merges the RDS endpoint
  (`dbHost`/`mysqlHost`) and bucket into `pipeline-install-config`.
- **IRSA service-account annotations** for `ml-pipeline`, `ml-pipeline-ui`, and
  `argo` carry the `eks.amazonaws.com/role-arn` placeholders.

The `mysql-secret` is **not** generated here — it is created out-of-band by the
provisioning layer (with `lifecycle ignore_changes` so the managed password
survives). `delete-mysql-secret.yaml` removes the base's placeholder secret so
the externally-managed one is authoritative.

`<changeme-…>` placeholders are substituted to environment variables
(`${cluster_name}`, `${rds_endpoint}`, the IRSA role ARNs) during manifest
generation downstream.
