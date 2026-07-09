# Generating the postera-devops Kubeflow manifests

The `manifests/kubeflow/*.yaml` files consumed by `postera-devops`
(`terraform/services/kubeflow/manifests/kubeflow/`) are generated from this
repo's `example/` kustomization. This is the record of how to regenerate them
after changing any overlay or patch here.

## Pipeline

```
example/ (+ all overlays/patches)  ──kustomize build──▶  one big YAML stream  ──split_by_kind.py──▶  <Kind>.yaml per kind
```

## Command

Pinned to **kustomize v5.4.3** (the version this repo's CI installs — see
`tests/kustomize_install.sh`). `split_by_kind.py` writes each file into the
current working directory, so run it from the devops output directory:

```sh
cd <postera-devops>/terraform/services/kubeflow/manifests/kubeflow
kustomize build <this-repo>/example | python3 <this-repo>/split_by_kind.py
```

`split_by_kind.py` groups documents by `kind`, kebab-cases the filename
(`AuthorizationPolicy` → `authorization-policy.yaml`), and — critically —
replaces every `${` with `$${`.

## Why the `${` → `$${` escaping

`postera-devops` renders these files as **Terraform templates**
(`kubectl_path_documents`), which interpolates `${...}`. Every `${...}` in this
repo's source is a KFP **runtime shell placeholder** (`${NAMESPACE}`,
`${TTL_SECONDS_AFTER_WORKFLOW_FINISH}`, `${NUM_WORKERS}`, `${EXECUTIONTYPE}`,
`${LOG_LEVEL}`) that the container's entrypoint expands at pod start — Terraform
must pass it through untouched. Escaping to `$${...}` makes Terraform emit a
literal `${...}`. So the blanket escape is correct for everything the script
produces.

## Manual post-generation steps

Regeneration overwrites `manifests/kubeflow/*`, so these edits must be re-applied
by hand each time:

1. **Inject the real Terraform var for the workflow TTL.** The persistence-agent
   Deployment's `TTL_SECONDS_AFTER_WORKFLOW_FINISH` env value ships from upstream
   base as the literal `"<changeme-ttl-seconds-after-workflow-finish>"`. Replace
   it with the Terraform variable, **single `$`** so TF interpolates it:

   ```yaml
   - name: TTL_SECONDS_AFTER_WORKFLOW_FINISH
     value: '${kfp_workflow_ttl_seconds}'
   ```

   This is the only genuine TF variable inside the generated set; leave every
   other `$${...}` escaped.

## Not part of this pipeline

`postera-devops` also carries `manifests/datadog-agent/` and
`manifests/karpenter/`. Those are **hand-authored** in the devops repo (single
files, not one-per-kind) and are **not** produced by `kustomize build` here — do
not regenerate or overwrite them from this repo.
