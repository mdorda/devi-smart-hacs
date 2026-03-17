from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def expected_tag() -> str:
    release_tag = os.environ.get("GITHUB_EVENT_RELEASE_TAG_NAME")
    if release_tag:
        return release_tag

    ref_name = os.environ.get("GITHUB_REF_NAME")
    if ref_name:
        return ref_name

    raise SystemExit("Could not determine the release tag from GitHub Actions environment.")


def manifest_version() -> str:
    manifest_path = Path("custom_components/devi/manifest.json")
    manifest = json.loads(manifest_path.read_text())
    return manifest["version"]


def main() -> int:
    tag = expected_tag()
    version = manifest_version()
    normalized_tag = tag[1:] if tag.startswith("v") else tag

    if normalized_tag != version:
        print(f"Manifest version {version} does not match release tag {tag}.", file=sys.stderr)
        return 1

    print(f"Release tag {tag} matches manifest version {version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
