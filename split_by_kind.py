import re
import sys
from collections import defaultdict

import yaml

DOCKER_HUB_MIRROR = "<changeme-docker-hub-mirror>"
MIRRORS = {
    "docker.io": DOCKER_HUB_MIRROR,
    "ghcr.io": "<changeme-ghcr-mirror>",
    "quay.io": "<changeme-quay-mirror>",
}

REF = r"[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*(?::[A-Za-z0-9._-]+)?(?:@sha256:[0-9a-f]{64})?"
EXPLICIT = re.compile(r"(?<![\w./-])(docker\.io|ghcr\.io|quay\.io)/(" + REF + r")")
IMAGE_KEY = re.compile(r"(?:^|[a-z_-])(?:image|Image|IMAGE|hub)$")
JSON_IMAGE = re.compile(r'("(?:image|[A-Za-z]+Image|hub)"\s*:\s*")(' + REF + r')(")')


def mirror_explicit(text, library=True):
    def sub(m):
        registry, path = m.group(1), m.group(2)
        if library and registry == "docker.io" and "/" not in path:
            path = "library/" + path
        return MIRRORS[registry] + "/" + path

    return EXPLICIT.sub(sub, text)


def implicit_docker_hub(ref):
    first = ref.split("/")[0]
    return bool(re.fullmatch(REF, ref)) and "/" in ref and "." not in first and ":" not in first and first != "localhost"


def mirror_image(ref, hub=False):
    ref = mirror_explicit(ref, library=not hub)
    return DOCKER_HUB_MIRROR + "/" + ref if implicit_docker_hub(ref) else ref


def mirror_text(text):
    text = JSON_IMAGE.sub(lambda m: m.group(1) + mirror_image(m.group(2), hub=m.group(1).startswith('"hub"')) + m.group(3), text)
    return mirror_explicit(text)


def mirror(node, key=""):
    if isinstance(node, dict):
        env_image = isinstance(node.get("name"), str) and IMAGE_KEY.search(node["name"])
        for k, v in node.items():
            if env_image and k == "value" and isinstance(v, str):
                node[k] = mirror_image(v)
            else:
                node[k] = mirror(v, k)
        return node
    if isinstance(node, list):
        out = []
        for i, item in enumerate(node):
            flag = node[i - 1] if i else None
            if isinstance(item, str) and isinstance(flag, str) and flag.endswith("-image"):
                out.append(mirror_image(item))
            else:
                out.append(mirror(item, key))
        return out
    if isinstance(node, str):
        return mirror_image(node, hub=key == "hub") if IMAGE_KEY.search(key) else mirror_text(node)
    return node


def unmirrored(node, key="", found=None):
    found = set() if found is None else found
    if isinstance(node, dict):
        for k, v in node.items():
            unmirrored(v, k, found)
    elif isinstance(node, list):
        for v in node:
            unmirrored(v, key, found)
    elif isinstance(node, str):
        found.update(m.group(0) for m in EXPLICIT.finditer(node))
        refs = [m.group(2) for m in JSON_IMAGE.finditer(node)]
        if IMAGE_KEY.search(key):
            refs.append(node)
        found.update(r for r in refs if implicit_docker_hub(r))
    return found


def kind_to_filename(kind):
    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1-\2", kind)
    kebab = re.sub("([a-z0-9])([A-Z])", r"\1-\2", s1).lower()
    return f"{kebab}.yaml"


docs = [mirror(d) for d in yaml.safe_load_all(sys.stdin) if d]

leftover = unmirrored(docs)
if leftover:
    sys.exit("images not rewritten to a mirror:\n  " + "\n  ".join(sorted(leftover)))

grouped = defaultdict(list)
for doc in docs:
    if "kind" in doc:
        grouped[doc["kind"]].append(doc)

# The output is rendered as Terraform templates, so escape every `${`.
for kind, resources in grouped.items():
    yaml_str = yaml.dump_all(resources).replace("${", "$${")
    with open(kind_to_filename(kind), "w") as f:
        f.write(yaml_str)
