"""MkDocs build hooks for publishing the verified CI artifact with Mike."""
import json
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def on_page_markdown(markdown, page, config, files):
    if page.file.src_uri.startswith("generated/"):
        # Generated overlays have no corresponding maintained Markdown file.
        page.edit_url = None


def on_post_build(config):
    prepared = os.getenv("FAXBOT_DOCS_BUILT_SITE")
    if not prepared:
        return
    artifact = Path(prepared).resolve(strict=True)
    target = Path(config["site_dir"]).resolve()
    if artifact == target or target in artifact.parents or artifact in target.parents:
        raise RuntimeError("Verified artifact and build directories must be separate.")
    provenance = json.loads((artifact / "generated/provenance.json").read_text())
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if provenance["source_sha"] != revision or provenance["source_tree_dirty"]:
        raise RuntimeError("Verified artifact does not match the clean checked-out source.")
    shutil.rmtree(target)
    shutil.copytree(artifact, target)
