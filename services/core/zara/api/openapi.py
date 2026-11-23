"""Document RFC 9457 error responses in the generated OpenAPI contract."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from zara.api.errors import FieldError, ProblemDetail


def _register_problem_schema(schemas: dict[str, Any]) -> None:
    problem_schema = ProblemDetail.model_json_schema(
        ref_template="#/components/schemas/{model}",
    )
    definitions = problem_schema.pop("$defs", {})
    schemas.update(definitions)
    schemas.setdefault("FieldError", FieldError.model_json_schema())
    schemas["ProblemDetail"] = problem_schema


def install_openapi(app: FastAPI) -> None:
    """Add a typed ``default`` problem+json response to every operation."""

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema

        schema = get_openapi(
            title=app.title,
            version=app.version,
            summary=app.summary,
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        _register_problem_schema(components)

        for path_item in schema.get("paths", {}).values():
            for method, operation in path_item.items():
                if method not in {"get", "put", "post", "delete", "patch", "options", "head"}:
                    continue
                operation.setdefault("responses", {})["default"] = {
                    "description": "RFC 9457 problem details",
                    "content": {
                        "application/problem+json": {
                            "schema": {"$ref": "#/components/schemas/ProblemDetail"}
                        }
                    },
                }

        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
