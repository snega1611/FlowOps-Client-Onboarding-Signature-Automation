import glob
import json
import re
import sys
from urllib.parse import urlsplit
from unittest.mock import Mock

sys.path.insert(0, ".")

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.fakes import FakeGmailAdapter  # noqa: E402


def norm(path):
    """Normalize query strings and FastAPI/n8n path parameters."""
    path = path.split("?", 1)[0]
    path = re.sub(r"\{\{.*?\}\}", "{x}", path)
    path = re.sub(r"\{[^/{}]*\}", "{x}", path)
    path = re.sub(r"/+", "/", path)
    if path != "/":
        path = path.rstrip("/")
    return path or "/"



def workflow_url_path(raw_url):
    """Extract a normalized path from static and dynamic n8n URLs."""
    raw_url = raw_url.strip().lstrip("=")

    # Embedded expressions:
    # http://localhost:8000/cases/{{ $json.case_id }}/documents
    if "{{" in raw_url and not raw_url.startswith("{{"):
        parsed = urlsplit(raw_url)
        path = parsed.path or "/"
        path = re.sub(r"\{\{.*?\}\}", "{x}", path)
        return norm(path)

    # Full expressions:
    # {{ 'http://localhost:8000/cases/' + $json.id + '/complete' }}
    if raw_url.startswith("{{"):
        expression = raw_url[2:]
        if expression.endswith("}}"):
            expression = expression[:-2]
        expression = expression.strip()

        # Remove the quoted URL's scheme and host, if present.
        expression = re.sub(
            r"""(['"])https?://[^/'"]+""",
            r"\1",
            expression,
        )

        # Replace a concatenated JSON variable with a path placeholder.
        expression = re.sub(
            r"""\s*\+\s*\$json\.[A-Za-z_]\w*\s*\+\s*""",
            "'{x}'",
            expression,
        )

        # Extract and concatenate the quoted path fragments.
        fragments = re.findall(r"""['"]([^'"]*)['"]""", expression)
        if not fragments:
            raise ValueError(
                f"Cannot parse n8n URL expression: {raw_url}"
            )

        return norm("".join(fragments))

    parsed = urlsplit(raw_url)
    if parsed.scheme and parsed.netloc:
        return norm(parsed.path or "/")

    return norm(raw_url)




app = create_app(
    Settings(db_path="data/validate.db"),
    gmail=FakeGmailAdapter(),
    signer=Mock(provider="opensign_sandbox", sandbox=True),
)

routes = {
    (method.upper(), norm(route.path))
    for route in app.routes
    if hasattr(route, "methods")
    for method in route.methods
}

errors = []
files = sorted(glob.glob("n8n/workflows/*.json"))

for file_path in files:
    with open(file_path, encoding="utf-8") as file:
        workflow = json.load(file)

    names = [node["name"] for node in workflow["nodes"]]

    if len(names) != len(set(names)):
        errors.append(f"{file_path}: duplicate node names")

    for source, outputs in workflow["connections"].items():
        if source not in names:
            errors.append(
                f"{file_path}: connection source '{source}' missing"
            )

        for branch in outputs.get("main", []):
            for connection in branch:
                if connection["node"] not in names:
                    errors.append(
                        f"{file_path}: connection target "
                        f"'{connection['node']}' missing"
                    )

    for node in workflow["nodes"]:
        if not node["type"].endswith("httpRequest"):
            continue

        raw_url = node["parameters"]["url"]
        method = node["parameters"].get("method", "GET").upper()

        try:
            path = workflow_url_path(raw_url)
        except ValueError as exc:
            errors.append(f"{file_path}: '{node['name']}': {exc}")
            continue

        if (method, path) not in routes:
            errors.append(
                f"{file_path}: '{node['name']}' -> "
                f"{method} {path} is not a FastAPI route"
            )

    triggers = {
        node["name"]
        for node in workflow["nodes"]
        if re.search(r"Trigger|webhook", node["type"], re.IGNORECASE)
    }

    seen = set(triggers)
    stack = list(triggers)

    while stack:
        current = stack.pop()
        outputs = workflow["connections"].get(current, {"main": []})

        for branch in outputs.get("main", []):
            for connection in branch:
                target = connection["node"]
                if target not in seen:
                    seen.add(target)
                    stack.append(target)

    for name in names:
        if name not in seen:
            errors.append(
                f"{file_path}: node '{name}' unreachable from any trigger"
            )

print(f"Validated {len(files)} workflows")
print(
    "\n".join(errors)
    or "OK: JSON valid, connections consistent, API paths match routes"
)
sys.exit(1 if errors else 0)
