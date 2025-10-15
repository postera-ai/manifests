import sys
import yaml
import re
from collections import defaultdict

# Convert kind to kebab-case filename
def kind_to_filename(kind):
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1-\2', kind)
    kebab = re.sub('([a-z0-9])([A-Z])', r'\1-\2', s1).lower()
    return f"{kebab}.yaml"

# Read all YAML documents from stdin
docs = list(yaml.safe_load_all(sys.stdin))

# Group by kind
grouped = defaultdict(list)
for doc in docs:
    if doc and 'kind' in doc:
        grouped[doc['kind']].append(doc)

# Write each kind to its own file with kebab-case filenames
for kind, resources in grouped.items():
    filename = kind_to_filename(kind)
    # Dump YAML to string, then replace `${` with `$${`
    yaml_str = yaml.dump_all(resources)
    yaml_str = yaml_str.replace('${', '$${')
    with open(filename, "w") as f:
        f.write(yaml_str)
