"""Generate the browser's consumed JSON DTOs from the registered FastAPI routes."""

import argparse
import json
from pathlib import Path

from apps.supportflow.api.main import app

DESTINATION = Path(__file__).with_name("product") / "contracts.ts"
# Alias, relative path, method, successful response status (or request).
CONTRACTS = (
    ("Actor", "me", "get", "200"),
    ("CsrfToken", "auth/csrf", "get", "200"),
    ("LoginInput", "auth/login", "post", "request"),
    ("LoginResult", "auth/login", "post", "200"),
    ("CustomersPage", "customers", "get", "200"),
    ("TicketsPage", "tickets", "get", "200"),
    ("TicketInput", "tickets", "post", "request"),
    ("Ticket", "tickets", "post", "201"),
    ("TicketDetail", "tickets/{ticket_id}", "get", "200"),
    ("ThreadInput", "threads", "post", "request"),
    ("Thread", "threads", "post", "201"),
    ("RunInput", "threads/{thread_id}/runs", "post", "request"),
    ("RunAccepted", "threads/{thread_id}/runs", "post", "202"),
    ("RunsPage", "threads/{thread_id}/runs", "get", "200"),
    ("RunDetail", "runs/{run_id}", "get", "200"),
    ("RunsIndexPage", "runs", "get", "200"),
    ("CancelInput", "runs/{run_id}/cancel", "post", "request"),
    ("CancelResult", "runs/{run_id}/cancel", "post", "200"),
    ("ClarificationSubmit", "runs/{run_id}/clarifications", "post", "request"),
    ("ClarificationAccepted", "runs/{run_id}/clarifications", "post", "202"),
    ("ApprovalsPage", "approvals", "get", "200"),
    ("ApprovalDetail", "approvals/{approval_id}", "get", "200"),
    ("ApprovalDecisionSubmit", "approvals/{approval_id}/decisions", "post", "request"),
    ("ApprovalDecisionResult", "approvals/{approval_id}/decisions", "post", "202"),
    ("RunEventsPage", "runs/{run_id}/events/history", "get", "200"),
    ("Citation", "citations/{evidence_id}", "get", "200"),
    ("DocumentsPage", "documents", "get", "200"),
    ("DocumentVersionsPage", "documents/{document_id}/versions", "get", "200"),
    ("DocumentStaged", "documents", "post", "201"),
    ("DocumentActivationInput", "documents/{document_id}/versions/{version_id}/activate", "post", "request"),
    ("DocumentActivationResult", "documents/{document_id}/versions/{version_id}/activate", "post", "200"),
    ("EvaluationReportsPage", "eval/reports", "get", "200"),
    ("EvaluationReport", "eval/reports/{report_id}", "get", "200"),
)


def ts_type(schema):
    if not schema:
        return "unknown"
    if "$ref" in schema:
        return schema["$ref"].split("/")[-1]
    if "enum" in schema:
        return " | ".join(json.dumps(value) for value in schema["enum"])
    if "const" in schema:
        return json.dumps(schema["const"])
    if "anyOf" in schema:
        return " | ".join(ts_type(option) for option in schema["anyOf"])
    if schema["type"] == "array":
        return f'Array<{ts_type(schema["items"])}>'
    if schema["type"] == "object":
        if schema.get("additionalProperties") is True:
            return "Record<string, unknown>"
        fields = []
        for key, field in schema["properties"].items():
            optional = "" if key in schema.get("required", []) else "?"
            fields.append(f"  {json.dumps(key)}{optional}: {ts_type(field)};")
        return "{\n" + "\n".join(fields) + "\n}" if fields else "Record<string, never>"
    return {"string": "string", "integer": "number", "number": "number",
            "boolean": "boolean", "null": "null"}[schema["type"]]


def generate():
    spec = app.openapi()
    components = spec["components"]["schemas"]
    aliases = []
    required = set()
    for alias, path, method, result in CONTRACTS:
        route = spec["paths"]["/api/v1/" + path][method]
        data = route["requestBody"] if result == "request" else route["responses"][result]
        name = ts_type(data["content"]["application/json"]["schema"])
        required.add(name)
        if alias != name:
            aliases.append(f"export type {alias} = {name};")

    def collect(schema):
        if isinstance(schema, list):
            for item in schema:
                collect(item)
        elif isinstance(schema, dict):
            if "$ref" in schema:
                name = ts_type(schema)
                if name not in required:
                    required.add(name)
                    collect(components[name])
            for value in schema.values():
                collect(value)

    for name in sorted(required):
        collect(components[name])
    return "\n\n".join([
        "// Generated from app.openapi() by python -m apps.supportflow.web.generate_types.",
        *(f"export type {name} = {ts_type(components[name])};" for name in sorted(required)),
        "\n".join(aliases),
    ]) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if committed types differ")
    args = parser.parse_args()
    generated = generate()
    if args.check:
        if DESTINATION.read_text(encoding="utf-8") != generated:
            parser.exit(1, "contracts.ts differs from the current OpenAPI\n")
    else:
        DESTINATION.write_text(generated, encoding="utf-8", newline="\n")
