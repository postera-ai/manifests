# Generating per-kind manifests

`split_by_kind.py` turns the `example/` build into one YAML file per kind, for
consumers that render the manifests as Terraform templates.

```sh
cd <output directory>
kustomize build <this-repo>/example | python3 <this-repo>/split_by_kind.py
```

Use kustomize v5.4.3, the version CI installs (`tests/kustomize_install.sh`).

The script:

- writes `<kind>.yaml` per kind into the current directory
  (`AuthorizationPolicy` → `authorization-policy.yaml`);
- rewrites every Docker Hub, GitHub Container Registry and Quay image to
  `<changeme-docker-hub-mirror>/…`, `<changeme-ghcr-mirror>/…` and
  `<changeme-quay-mirror>/…`, including images inside ConfigMap values, and
  exits non-zero if any are left;
- escapes every `${` to `$${`, because every `${...}` in the source is a
  container runtime placeholder (`${NAMESPACE}`, `${NUM_WORKERS}`, …) that
  Terraform must pass through.

After generating, replace every `<changeme-…>` placeholder with the matching
single-`$` Terraform variable, for example:

```yaml
- name: TTL_SECONDS_AFTER_WORKFLOW_FINISH
  value: '${kfp_workflow_ttl_seconds}'   # was "<changeme-ttl-seconds-after-workflow-finish>"
```

`grep changeme` on the output must return nothing.
