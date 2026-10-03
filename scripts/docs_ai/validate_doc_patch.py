#!/usr/bin/env python3
"""Keep optional LLM patches in maintained instructional Markdown only."""
from pathlib import PurePosixPath
import subprocess
import sys


def validate(path):
    result = subprocess.check_output(["git", "apply", "--numstat", path], text=True)
    if not result.strip():
        raise ValueError("Documentation proposal contains no changes.")
    for line in result.splitlines():
        name = line.split("\t", 2)[2]
        candidate = PurePosixPath(name)
        if (candidate.suffix != ".md" or not candidate.parts or candidate.parts[0] != "docs"
                or ".." in candidate.parts or candidate.is_absolute()
                or candidate.parts[1:2] in [("generated",), ("superpowers",), ("modernization",)]):
            raise ValueError("Documentation proposal exceeds maintained Markdown scope.")


if __name__ == "__main__":
    validate(sys.argv[1])
