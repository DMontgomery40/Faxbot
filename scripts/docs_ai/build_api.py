"""Build a self-hosted Redocly client page from the generated source contract."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess

import yaml


TOOLCHAIN = Path(__file__).resolve().parent


def build(output):
    output = Path(output).resolve()
    modules = TOOLCHAIN / "node_modules"
    expected = {"redoc": "2.5.0", "react": "18.3.1", "react-dom": "18.3.1", "styled-components": "5.3.11"}
    for name, version in expected.items():
        actual = json.loads((modules / name / "package.json").read_text())["version"]
        if actual != version:
            raise RuntimeError(f"Redoc toolchain mismatch for {name}: {actual}; run npm ci.")
    provenance = json.loads((output / "provenance.json").read_text())
    options = yaml.safe_load((TOOLCHAIN / "redocly.yaml").read_text())["theme"]["openapi"]
    # The supported custom template uses Redoc.init rather than the CLI's SSR
    # hydration tree. The npm package's browser bundle renders the local spec.
    subprocess.run([
        str(modules / ".bin/redocly"), "build-docs", str(output / "openapi.json"),
        "--output", str(output / "api.html"), "--config", str(TOOLCHAIN / "redocly.yaml"),
        "--disableGoogleFont", "--template", str(TOOLCHAIN / "redoc-client.hbs"),
        "--templateOptions.redocOptions", json.dumps(options).replace("</", "<\\/"),
        "--templateOptions.sourceSha", provenance["source_sha"],
        "--templateOptions.dirtyNote", "(includes uncommitted local changes)" if provenance["source_tree_dirty"] else "",
    ], check=True)
    for name in ("redoc.standalone.js", "redoc.standalone.js.LICENSE.txt"):
        shutil.copyfile(modules / "redoc/bundles" / name, output / name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", default="docs/generated")
    build(parser.parse_args().output)
