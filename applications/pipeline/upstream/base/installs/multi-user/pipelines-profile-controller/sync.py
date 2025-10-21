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
import yaml
import os
import base64


def main():
    settings = get_settings_from_env()
    server = server_factory(**settings)
    server.serve_forever()


def get_settings_from_env(controller_port=None,
                          visualization_server_image=None, frontend_image=None,
                          visualization_server_tag=None, frontend_tag=None, disable_istio_sidecar=None,
                          kfp_default_pipeline_root=None):
    """
    Returns a dict of settings from environment variables relevant to the controller

    Environment settings can be overridden by passing them here as arguments.

    Settings are pulled from the all-caps version of the setting name.  The
    following defaults are used if those environment variables are not set
    to enable backwards compatibility with previous versions of this script:
        visualization_server_image: ghcr.io/kubeflow/kfp-visualization-server
        visualization_server_tag: value of KFP_VERSION environment variable
        frontend_image: ghcr.io/kubeflow/kfp-frontend
        frontend_tag: value of KFP_VERSION environment variable
        disable_istio_sidecar: Required (no default)
    """
    settings = dict()
    settings["controller_port"] = \
        controller_port or \
        os.environ.get("CONTROLLER_PORT", "8080")

    settings["visualization_server_image"] = \
        visualization_server_image or \
        os.environ.get("VISUALIZATION_SERVER_IMAGE", "ghcr.io/kubeflow/kfp-visualization-server")

    settings["frontend_image"] = \
        frontend_image or \
        os.environ.get("FRONTEND_IMAGE", "ghcr.io/kubeflow/kfp-frontend")

    # Look for specific tags for each image first, falling back to
    # previously used KFP_VERSION environment variable for backwards
    # compatibility
    settings["visualization_server_tag"] = \
        visualization_server_tag or \
        os.environ.get("VISUALIZATION_SERVER_TAG") or \
        os.environ["KFP_VERSION"]

    settings["frontend_tag"] = \
        frontend_tag or \
        os.environ.get("FRONTEND_TAG") or \
        os.environ["KFP_VERSION"]

    settings["disable_istio_sidecar"] = \
        disable_istio_sidecar if disable_istio_sidecar is not None \
            else os.environ.get("DISABLE_ISTIO_SIDECAR") == "true"

    # KFP_DEFAULT_PIPELINE_ROOT is optional
    settings["kfp_default_pipeline_root"] = \
        kfp_default_pipeline_root or \
        os.environ.get("KFP_DEFAULT_PIPELINE_ROOT")

    return settings


def server_factory(visualization_server_image,
                   visualization_server_tag, frontend_image, frontend_tag,
                   disable_istio_sidecar, kfp_default_pipeline_root=None,
                   url="", controller_port=8080):
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

            desired_configmap_count = 1
            desired_resources = []
            if kfp_default_pipeline_root:
                desired_configmap_count = 2
                desired_resources += [{
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": "kfp-launcher",
                        "namespace": namespace,
                    },
                    "data": {
                        "defaultPipelineRoot": f"{kfp_default_pipeline_root}/{namespace}",
                        "providers": yaml.dump({
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
                    },
                }]


            # Compute status based on observed state.
            desired_status = {
                "kubeflow-pipelines-ready":
                    len(attachments["Secret.v1"]) == 1 and
                    len(attachments["ConfigMap.v1"]) == desired_configmap_count and
                    len(attachments["Deployment.apps/v1"]) == 2 and
                    len(attachments["Service.v1"]) == 2 and
                    len(attachments["DestinationRule.networking.istio.io/v1alpha3"]) == 1 and
                    len(attachments["AuthorizationPolicy.security.istio.io/v1beta1"]) == 2 and
                    "True" or "False"
            }

            # Generate the desired attachment object(s).
            desired_resources += [
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
                # Visualization server related manifests below
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "metadata": {
                        "labels": {
                            "app": "ml-pipeline-visualizationserver"
                        },
                        "name": "ml-pipeline-visualizationserver",
                        "namespace": namespace,
                    },
                    "spec": {
                        "selector": {
                            "matchLabels": {
                                "app": "ml-pipeline-visualizationserver"
                            },
                        },
                        "template": {
                            "metadata": {
                                "labels": {
                                    "app": "ml-pipeline-visualizationserver"
                                },
                                "annotations": disable_istio_sidecar and {
                                    "sidecar.istio.io/inject": "false"
                                } or {},
                            },
                            "spec": {
                                "containers": [{
                                    "image": f"{visualization_server_image}:{visualization_server_tag}",
                                    "imagePullPolicy":
                                        "IfNotPresent",
                                    "name":
                                        "ml-pipeline-visualizationserver",
                                    "ports": [{
                                        "containerPort": 8888
                                    }],
                                    "resources": {
                                        "requests": {
                                            "cpu": "50m",
                                            "memory": "200Mi"
                                        },
                                        "limits": {
                                            "cpu": "500m",
                                            "memory": "1Gi"
                                        },
                                    }
                                }],
                                "serviceAccountName":
                                    "default-editor",
                            },
                        },
                    },
                },
                {
                    "apiVersion": "networking.istio.io/v1alpha3",
                    "kind": "DestinationRule",
                    "metadata": {
                        "name": "ml-pipeline-visualizationserver",
                        "namespace": namespace,
                    },
                    "spec": {
                        "host": "ml-pipeline-visualizationserver",
                        "trafficPolicy": {
                            "tls": {
                                "mode": "ISTIO_MUTUAL"
                            }
                        }
                    }
                },
                {
                    "apiVersion": "security.istio.io/v1beta1",
                    "kind": "AuthorizationPolicy",
                    "metadata": {
                        "name": "ml-pipeline-visualizationserver",
                        "namespace": namespace,
                    },
                    "spec": {
                        "selector": {
                            "matchLabels": {
                                "app": "ml-pipeline-visualizationserver"
                            }
                        },
                        "rules": [{
                            "from": [{
                                "source": {
                                    "principals": ["cluster.local/ns/kubeflow/sa/ml-pipeline"]
                                }
                            }]
                        }]
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
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {
                        "name": "ml-pipeline-visualizationserver",
                        "namespace": namespace,
                    },
                    "spec": {
                        "ports": [{
                            "name": "http",
                            "port": 8888,
                            "protocol": "TCP",
                            "targetPort": 8888,
                        }],
                        "selector": {
                            "app": "ml-pipeline-visualizationserver",
                        },
                    },
                },
                # Artifact fetcher related resources below.
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
                                "annotations": disable_istio_sidecar and {
                                    "sidecar.istio.io/inject": "false"
                                } or {},
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
                                            "name": "MINIO_NAMESPACE",
                                            "value": ""
                                        },
                                        {
                                            "name": "MINIO_HOST",
                                            "value": "s3.us-west-2.amazonaws.com"
                                        },
                                        {
                                            "name": "MINIO_PORT",
                                            "value": ""
                                        },
                                        {
                                            "name": "MINIO_SSL",
                                            "value": "true"
                                        },
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
                                            "name": "AWS_ACCESS_KEY_ID",
                                            "value": ""
                                        },
                                        {
                                            "name": "AWS_SECRET_ACCESS_KEY",
                                            "value": ""
                                        },
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
            ]
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
