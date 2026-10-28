import os
from unittest import mock
import threading
from sync import get_settings_from_env, server_factory
import json

import pytest
import requests

# The DecoratorController posts {"object": <namespace>, "attachments": {...}} and
# the hook reports readiness by counting the attachments it observes against what
# it attaches. This controller attaches, per pipeline-enabled namespace:
#   ConfigMap x3        kfp-launcher, metadata-grpc-configmap, artifact-repositories
#   AuthorizationPolicy x2  allow-oauth2-proxy-to-all-{predictors,knative-services}
#   Deployment/Service x1   ml-pipeline-ui-artifact, ONLY when ARTIFACTS_PROXY_ENABLED
#   Secret x0           none (S3 auth is IRSA, no static-key secret)
# There is no DestinationRule and no visualization-server (dropped upstream).

KFP_VERSION = "x.y.z"
FRONTEND_IMAGE = "frontend-image"
FRONTEND_TAG = "somehash"
PIPELINE_ROOT = "s3://sandbox-kubeflow/v2/artifacts"

ENV_BASE = {
    "CONTROLLER_PORT": "0",  # HTTPServer picks a free port
    "KFP_VERSION": KFP_VERSION,
    "FRONTEND_IMAGE": FRONTEND_IMAGE,
    "FRONTEND_TAG": FRONTEND_TAG,
    "DISABLE_ISTIO_SIDECAR": "false",
    "KFP_DEFAULT_PIPELINE_ROOT": PIPELINE_ROOT,
}
ENV_PROXY_OFF = dict(ENV_BASE, ARTIFACTS_PROXY_ENABLED="false")
ENV_PROXY_ON = dict(ENV_BASE, ARTIFACTS_PROXY_ENABLED="true")


def _parent():
    return {
        "metadata": {
            "labels": {"pipelines.kubeflow.org/enabled": "true"},
            "name": "myName",
        }
    }


def _attachments(secret=0, configmap=0, deployment=0, service=0, authpolicy=0):
    def items(n):
        return {str(i): {} for i in range(n)}
    return {
        "Secret.v1": items(secret),
        "ConfigMap.v1": items(configmap),
        "Deployment.apps/v1": items(deployment),
        "Service.v1": items(service),
        "AuthorizationPolicy.security.istio.io/v1beta1": items(authpolicy),
    }


# Attachment sets that exactly match what the hook emits → ready=True
CORRECT_PROXY_OFF = _attachments(secret=0, configmap=3, deployment=0, service=0, authpolicy=2)
CORRECT_PROXY_ON = _attachments(secret=0, configmap=3, deployment=1, service=1, authpolicy=2)
# Wrong counts → ready=False
INCORRECT = _attachments(secret=1, configmap=1, deployment=0, service=0, authpolicy=1)

DATA_MISSING_PIPELINE_ENABLED = {"object": {}, "attachments": {}}


@pytest.fixture(scope="function")
def sync_server(request):
    """Start the sync HTTP server for a given environment on a daemon thread."""
    environ = request.param
    with mock.patch.dict(os.environ, environ):
        settings = get_settings_from_env()
        server = server_factory(**settings)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        yield server, environ
        server.shutdown()


def _post(server, observed):
    url = f"http://{server.server_address[0]}:{server.server_address[1]}"
    resp = requests.post(url, data=json.dumps(observed))
    return json.loads(resp.text)


@pytest.mark.parametrize(
    "sync_server, attachments, expected_ready",
    [
        (ENV_PROXY_OFF, CORRECT_PROXY_OFF, "True"),
        (ENV_PROXY_OFF, INCORRECT, "False"),
        (ENV_PROXY_ON, CORRECT_PROXY_ON, "True"),
        # proxy ON but no Deployment/Service observed yet → not ready
        (ENV_PROXY_ON, CORRECT_PROXY_OFF, "False"),
    ],
    indirect=["sync_server"],
)
def test_readiness_counts(sync_server, attachments, expected_ready):
    server, _ = sync_server
    results = _post(server, {"object": _parent(), "attachments": attachments})
    assert results["status"]["kubeflow-pipelines-ready"] == expected_ready


@pytest.mark.parametrize("sync_server", [ENV_PROXY_OFF], indirect=["sync_server"])
def test_attaches_irsa_resources_and_no_static_keys(sync_server):
    server, _ = sync_server
    results = _post(server, {"object": _parent(), "attachments": _attachments()})
    attached = results["attachments"]

    by_name = {r["metadata"]["name"]: r for r in attached}

    # No per-namespace static-credential secret, no visualization server.
    assert "mlpipeline-minio-artifact" not in by_name
    assert not any(r["kind"] == "Secret" for r in attached)
    assert not any("visualizationserver" in n for n in by_name)

    # kfp-launcher uses IRSA (credentials.fromEnv: true).
    providers = json.loads(by_name["kfp-launcher"]["data"]["providers"])
    assert providers["s3"]["default"]["credentials"]["fromEnv"] is True
    assert by_name["kfp-launcher"]["data"]["defaultPipelineRoot"] == f"{PIPELINE_ROOT}/myName"

    # Argo artifact repository uses the SDK credential chain, never static keys.
    repo = json.loads(by_name["artifact-repositories"]["data"]["default-namespaced"])
    assert repo["s3"]["useSDKCreds"] is True
    assert "accessKeySecret" not in repo["s3"]
    assert "secretKeySecret" not in repo["s3"]

    # Both oauth2-proxy ALLOW policies are present with the right selectors.
    assert by_name["allow-oauth2-proxy-to-all-predictors"]["spec"]["selector"]["matchLabels"]["component"] == "predictor"
    assert by_name["allow-oauth2-proxy-to-all-knative-services"]["spec"]["selector"]["matchLabels"]["component"] == "knative-service"


@pytest.mark.parametrize("sync_server", [ENV_PROXY_ON], indirect=["sync_server"])
def test_proxy_enabled_attaches_fetcher_without_secret_refs(sync_server):
    server, _ = sync_server
    results = _post(server, {"object": _parent(), "attachments": _attachments()})
    # Deployment and Service share the name ml-pipeline-ui-artifact, so key by kind.
    by_kind_name = {(r["kind"], r["metadata"]["name"]): r for r in results["attachments"]}

    fetcher = by_kind_name[("Deployment", "ml-pipeline-ui-artifact")]
    assert ("Service", "ml-pipeline-ui-artifact") in by_kind_name
    container = fetcher["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e for e in container["env"]}
    # MINIO keys are present but empty (IRSA), never sourced from a secret.
    assert env["MINIO_ACCESS_KEY"]["value"] == ""
    assert env["MINIO_SECRET_KEY"]["value"] == ""
    assert "valueFrom" not in env["MINIO_ACCESS_KEY"]
    assert "valueFrom" not in env["MINIO_SECRET_KEY"]
    assert container["image"] == f"{FRONTEND_IMAGE}:{FRONTEND_TAG}"


@pytest.mark.parametrize("sync_server", [ENV_PROXY_OFF], indirect=["sync_server"])
def test_pipeline_not_enabled_returns_empty(sync_server):
    server, _ = sync_server
    results = _post(server, DATA_MISSING_PIPELINE_ENABLED)
    assert results["status"] == {}
    assert results["attachments"] == []
