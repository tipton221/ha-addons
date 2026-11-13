"""Raise the patch number of an add-on's version in config.yaml. Prints the new version."""

import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()
match = re.search(r'^version: "(\d+)\.(\d+)\.(\d+)"$', text, re.M)
if not match:
    sys.exit(f"No version line in {path}")
major, minor, patch = (int(x) for x in match.groups())
new = f"{major}.{minor}.{patch + 1}"
path.write_text(text[: match.start()] + f'version: "{new}"' + text[match.end():])
print(new)
