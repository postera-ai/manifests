# Copyright 2020-2021 The Kubeflow Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import hashlib

# This overlay targets external S3 (via IRSA) + RDS, not the in-cluster SeaweedFS
# that upstream's profile controller provisions. We therefore drop upstream's
# botocore IAM/S3 client machinery (per-namespace access-key creation, the
# mlpipeline-minio-artifact Secret, and the bucket lifecycle policy): artifact
# auth is the pod's IRSA role from the default credential chain, not static keys.


def _normalize_domain(domain):
    return domain if domain.startswith('.') else '.' + domain


def main():
    settings = get_settings_from_env()
    server = server_factory(**settings)
    server.serve_forever()


def get_settings_from_env(controller_port=None,
                          frontend_image=None,
                          frontend_tag=None,
                          disable_istio_sidecar=None,
                          artifacts_proxy_enabled=None,
                          cluster_domain=None,
                          kfp_default_pipeline_root=None):
    """
    Returns a dict of settings from environment variables relevant to the controller

    Environment settings can be overridden by passing them here as arguments.

    Settings are pulled from the all-caps version of the setting name.  The
    following defaults are used if those environment variables are not set
    to enable backwards compatibility with previous versions of this script:
        frontend_image: ghcr.io/kubeflow/kfp-frontend
        frontend_tag: value of KFP_VERSION environment variable
        disable_istio_sidecar: Required (no default)
    """
    settings = dict()
    settings["controller_port"] = \
        controller_port or \
        os.environ.get("CONTROLLER_PORT", "8080")

    settings["frontend_image"] = \
        frontend_image or \
        os.environ.get("FRONTEND_IMAGE", "ghcr.io/kubeflow/kfp-frontend")

    settings["artifacts_proxy_enabled"] = \
        artifacts_proxy_enabled or \
        os.environ.get("ARTIFACTS_PROXY_ENABLED", "false")

    settings["cluster_domain"] = \
        cluster_domain or \
        os.environ.get("CLUSTER_DOMAIN", ".svc.cluster.local")

    # Look for specific tags for each image first, falling back to
    # previously used KFP_VERSION environment variable for backwards
    # compatibility
    settings["frontend_tag"] = \
        frontend_tag or \
        os.environ.get("FRONTEND_TAG") or \
        os.environ["KFP_VERSION"]

    settings["disable_istio_sidecar"] = \
        disable_istio_sidecar if disable_istio_sidecar is not None \
            else os.environ.get("DISABLE_ISTIO_SIDECAR") == "true"

    # KFP_DEFAULT_PIPELINE_ROOT is the external S3 root, e.g.
    # s3://<bucket>/v2/artifacts (wired from pipeline-install-config).
    settings["kfp_default_pipeline_root"] = \
        kfp_default_pipeline_root or \
        os.environ.get("KFP_DEFAULT_PIPELINE_ROOT")

    return settings


def server_factory(frontend_image,
                   frontend_tag,
                   disable_istio_sidecar,
                   artifacts_proxy_enabled,
                   cluster_domain=".svc.cluster.local",
                   kfp_default_pipeline_root=None,
                   url="",
                   controller_port=8080):
    """
    Returns an HTTPServer populated with Handler with customized settings
    """
    class Controller(BaseHTTPRequestHandler):
        def sync(self, parent, attachments):
            # parent is a namespace
            namespace = parent.get("metadata", {}).get("name")

            pipeline_enabled = parent.get("metadata", {}).get(
                "labels", {}).get("pipelines.kubeflow.org/enabled")

            if pipeline_enabled != "true":
                return {"status": {}, "attachments": []}

            proxy_enabled = artifacts_proxy_enabled.lower() == "true"

            # Compute status based on observed state.
            #
            # Counts MUST match exactly what this hook attaches, or every profile
            # namespace stays kubeflow-pipelines-ready=False:
            #   Secret == 0          no per-namespace secret (IRSA, no static keys)
            #   ConfigMap == 3       kfp-launcher + metadata-grpc-configmap + artifact-repositories
            #   AuthorizationPolicy == 2  the two oauth2-proxy ALLOW policies below
            #   Deployment/Service   only the artifact fetcher, gated on the proxy flag
            # No DestinationRule clause: the visualization-server (and its mTLS
            # DestinationRule) was dropped upstream and we do not re-add it.
            desired_status = {
                "kubeflow-pipelines-ready":
                    len(attachments["Secret.v1"]) == 0 and
                    len(attachments["ConfigMap.v1"]) == 3 and
                    len(attachments["Deployment.apps/v1"]) == (1 if proxy_enabled else 0) and
                    len(attachments["Service.v1"]) == (1 if proxy_enabled else 0) and
                    len(attachments["AuthorizationPolicy.security.istio.io/v1beta1"]) == 2 and
                    "True" or "False"
            }

            # The kfp-launcher S3 provider uses the pod's IRSA role
            # (credentials.fromEnv: true) — no static access/secret keys.
            kfp_launcher_data = {
                "defaultPipelineRoot": f"{kfp_default_pipeline_root}/{namespace}",
                "clusterDomain": cluster_domain,
                "providers": json.dumps({
                    "s3": {
                        "default": {
                            "endpoint": "s3.us-west-2.amazonaws.com",
                            "disableSSL": False,
                            "region": "us-west-2",
                            "forcePathStyle": True,
                            "credentials": {
                                "fromEnv": True
                            },
                        }
                    }
                })
            }

            # Argo's per-namespace artifact repository, pointing at external S3
            # via the SDK credential chain (IRSA). useSDKCreds replaces the
            # accessKeySecret/secretKeySecret refs upstream used for SeaweedFS.
            # Bucket is taken from KFP_DEFAULT_PIPELINE_ROOT (s3://<bucket>/...).
            artifact_bucket = \
                kfp_default_pipeline_root.split("/")[2] if kfp_default_pipeline_root else ""
            artifact_repository = {
                "archiveLogs": True,
                "s3": {
                    "endpoint": "s3.us-west-2.amazonaws.com",
                    "bucket": artifact_bucket,
                    "region": "us-west-2",
                    "useSDKCreds": True,
                    "keyFormat": f"artifacts/{namespace}/{{{{workflow.creationTimestamp.Y}}}}/{{{{workflow.creationTimestamp.m}}}}/{{{{workflow.creationTimestamp.d}}}}/{{{{pod.name}}}}",
                }
            }

            # Generate the desired attachment object(s).
            desired_resources = [
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": "kfp-launcher",
                        "namespace": namespace,
                    },
                    "data": kfp_launcher_data,
                },
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": "metadata-grpc-configmap",
                        "namespace": namespace,
                    },
                    "data": {
                        "METADATA_GRPC_SERVICE_HOST":
                            "metadata-grpc-service.kubeflow",
                        "METADATA_GRPC_SERVICE_PORT": "8080",
                    },
                },
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": "artifact-repositories",
                        "namespace": namespace,
                        "annotations": {
                            "workflows.argoproj.io/default-artifact-repository": "default-namespaced"
                        }
                    },
                    "data": {
                        "default-namespaced": json.dumps(artifact_repository)
                    }
                },
                # Added to allow all oauth2-proxy auth'ed requests to access KServe inference service predictors
                {
                    "apiVersion": "security.istio.io/v1beta1",
                    "kind": "AuthorizationPolicy",
                    "metadata": {
                        "name": "allow-oauth2-proxy-to-all-predictors",
                        "namespace": namespace,
                    },
                    "spec": {
                        "action": "ALLOW",
                        "selector": {
                            "matchLabels": {
                                "component": "predictor"
                            }
                        },
                        "rules": [{}]
                    }
                },
                # Parallel to the predictor policy above, but for plain Knative
                # Services (ksvc) that aren't KServe InferenceServices. They sit in
                # the same namespace default-deny and carry `component: knative-service`
                # instead of `component: predictor`, so they need their own ALLOW.
                {
                    "apiVersion": "security.istio.io/v1beta1",
                    "kind": "AuthorizationPolicy",
                    "metadata": {
                        "name": "allow-oauth2-proxy-to-all-knative-services",
                        "namespace": namespace,
                    },
                    "spec": {
                        "action": "ALLOW",
                        "selector": {
                            "matchLabels": {
                                "component": "knative-service"
                            }
                        },
                        "rules": [{}]
                    }
                },
            ]

            # Add artifact fetcher related resources if enabled. The fetcher
            # talks to S3 via IRSA (the namespace's default-editor SA role), so
            # the MINIO_* access keys are left empty rather than sourced from a
            # secret.
            if proxy_enabled:
                desired_resources.extend([
                    {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "metadata": {
                            "labels": {
                                "app": "ml-pipeline-ui-artifact"
                            },
                            "name": "ml-pipeline-ui-artifact",
                            "namespace": namespace,
                        },
                        "spec": {
                            "selector": {
                                "matchLabels": {
                                    "app": "ml-pipeline-ui-artifact"
                                }
                            },
                            "template": {
                                "metadata": {
                                    "labels": {
                                        "app": "ml-pipeline-ui-artifact"
                                    },
                                    "annotations": {
                                        **({"sidecar.istio.io/inject": "false"} if disable_istio_sidecar else {}),
                                        "kubeflow-pipelines/image-spec-hash": hashlib.sha256(f"{frontend_image}:{frontend_tag}".encode()).hexdigest()[:16],
                                    },
                                },
                                "spec": {
                                    "containers": [{
                                        "name":
                                            "ml-pipeline-ui-artifact",
                                        "image": f"{frontend_image}:{frontend_tag}",
                                        "imagePullPolicy":
                                            "IfNotPresent",
                                        "ports": [{
                                            "containerPort": 3000
                                        }],
                                        "env": [
                                            {
                                                "name": "MINIO_ACCESS_KEY",
                                                "value": ""
                                            },
                                            {
                                                "name": "MINIO_SECRET_KEY",
                                                "value": ""
                                            },
                                            {
                                                "name": "AWS_REGION",
                                                "value": "us-west-2"
                                            },
                                            {
                                                "name": "AWS_S3_ENDPOINT",
                                                "value": "s3.us-west-2.amazonaws.com"
                                            },
                                            {
                                                "name": "AWS_SSL",
                                                "value": "true"
                                            },
                                            {
                                                "name": "ML_PIPELINE_SERVICE_HOST",
                                                "value": f"ml-pipeline.kubeflow{_normalize_domain(cluster_domain)}"
                                            },
                                            {
                                                "name": "ML_PIPELINE_SERVICE_PORT",
                                                "value": "8888"
                                            },
                                            {
                                                "name": "FRONTEND_SERVER_NAMESPACE",
                                                "value": namespace,
                                            },
                                            {
                                                "name": "CLUSTER_DOMAIN",
                                                "value": cluster_domain,
                                            }
                                        ],
                                        "resources": {
                                            "requests": {
                                                "cpu": "10m",
                                                "memory": "70Mi"
                                            },
                                            "limits": {
                                                "cpu": "100m",
                                                "memory": "500Mi"
                                            },
                                        }
                                    }],
                                    "serviceAccountName":
                                        "default-editor"
                                }
                            }
                        }
                    },
                    {
                        "apiVersion": "v1",
                        "kind": "Service",
                        "metadata": {
                            "name": "ml-pipeline-ui-artifact",
                            "namespace": namespace,
                            "labels": {
                                "app": "ml-pipeline-ui-artifact"
                            }
                        },
                        "spec": {
                            "ports": [{
                                "name":
                                    "http",  # name is required to let istio understand request protocol
                                "port": 80,
                                "protocol": "TCP",
                                "targetPort": 3000
                            }],
                            "selector": {
                                "app": "ml-pipeline-ui-artifact"
                            }
                        }
                    },
                ])

            print('Received request:\n', json.dumps(parent, sort_keys=True))
            print('Desired resources except secrets:\n', json.dumps(desired_resources, sort_keys=True))

            return {"status": desired_status, "attachments": desired_resources}

        def do_POST(self):
            # Serve the sync() function as a JSON webhook.
            observed = json.loads(
                self.rfile.read(int(self.headers.get("content-length"))))
            desired = self.sync(observed["object"], observed["attachments"])

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(bytes(json.dumps(desired), 'utf-8'))

    return HTTPServer((url, int(controller_port)), Controller)


if __name__ == "__main__":
    main()
