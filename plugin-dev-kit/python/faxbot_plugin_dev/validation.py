import re
import json
from typing import Dict, Any, List
from jsonschema import Draft7Validator


def validate_manifest(manifest: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    required = ["id", "name", "version", "description", "author", "categories", "capabilities"]
    for f in required:
        if f not in manifest:
            errors.append(f"Missing required field: {f}")
    if "id" in manifest and not re.match(r"^[a-z0-9-]+$", str(manifest["id"])):
        errors.append("ID must be lowercase letters, numbers, and hyphens only")
    if "version" in manifest and not re.match(r"^\d+\.\d+\.\d+", str(manifest["version"])):
        errors.append("Version must be semver format (e.g., 1.0.0)")
    valid_categories = ["outbound", "inbound", "storage", "auth", "messaging", "transform", "custom"]
    if "categories" in manifest:
        for cat in manifest["categories"]:
            if cat not in valid_categories:
                errors.append(f"Invalid category: {cat}")
    expected_caps = {
        "outbound": ["send_fax", "get_status"],
        "inbound": ["receive_fax", "list_inbound"],
        "storage": ["put", "get", "delete"],
        "auth": ["authenticate", "validate_token"],
        "messaging": ["send_message", "get_message_status"],
    }
    if "categories" in manifest and "capabilities" in manifest:
        for cat in manifest["categories"]:
            if cat in expected_caps and not any(c in manifest["capabilities"] for c in expected_caps[cat]):
                errors.append(f"Category {cat} requires at least one of: {expected_caps[cat]}")
    return errors


def validate_config_schema(schema: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    try:
        Draft7Validator.check_schema(schema)
    except Exception as e:
        errors.append(f"Invalid JSON Schema: {e}")
        return errors
    # Allow $ref in development; production policies should be enforced server-side
    s = json.dumps(schema).lower()
    for field in ["ssn", "social_security", "patient_name", "dob", "date_of_birth"]:
        if field in s:
            errors.append(f"Config schema cannot contain PHI field: {field}")
    return errors


def check_hipaa_compliance(code: str) -> List[str]:
    warnings: List[str] = []
    if re.search(r'log.*\(\s*["\'].*\d{3}[-.]?\d{2}[-.]?\d{4}', code):
        warnings.append("Possible SSN in log statement")
    if re.search(r'log.*\(\s*["\'].*\d{10}', code):
        warnings.append("Possible phone number in log statement")
    if re.search(r'print\s*\([^)]*to_number', code):
        warnings.append("Printing phone number - use mask_phone_number()")
    if re.search(r'config\s*\[.*patient|ssn|dob', code, re.IGNORECASE):
        warnings.append("Possible PHI being stored in configuration")
    if "open(" in code and "encrypt" not in code:
        warnings.append("File operations should use encryption for PHI")
    return warnings


def validate_plugin_package(package_path: str) -> Dict[str, Any]:
    from pathlib import Path
    result: Dict[str, Any] = {"valid": True, "errors": [], "warnings": []}
    p = Path(package_path)
    for f in ["manifest.json", "README.md", "__init__.py"]:
        if not (p / f).exists():
            result["errors"].append(f"Missing required file: {f}")
            result["valid"] = False
    if (p / "manifest.json").exists():
        try:
            manifest = json.loads((p / "manifest.json").read_text())
        except Exception as e:
            result["errors"].append(f"Failed to parse manifest.json: {e}")
            result["valid"] = False
            return result
        errs = validate_manifest(manifest)
        if errs:
            result["errors"].extend(errs)
            result["valid"] = False
        if manifest.get("config_schema"):
            serrs = validate_config_schema(manifest["config_schema"])
            if serrs:
                result["errors"].extend(serrs)
                result["valid"] = False
    for py in p.glob("**/*.py"):
        code = py.read_text(errors="ignore")
        warns = check_hipaa_compliance(code)
        result["warnings"].extend([f"{py.name}: {w}" for w in warns])
    return result

