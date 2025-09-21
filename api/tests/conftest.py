import os
import sys

# Ensure project root is on sys.path for `import api.*`
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Ensure test mode flag for Ghostscript-less CI/unit tests
os.environ.setdefault("FAXBOT_TEST_MODE", "true")
