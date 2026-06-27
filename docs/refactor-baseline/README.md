# Refactor Baseline

This folder captures the API surface before the modular refactor.

- `routes.json` lists FastAPI routes by path, methods, route name, endpoint, and tags.
- `openapi.json` is the generated OpenAPI schema.
- `summary.json` records route and OpenAPI path counts.

Regenerate after each refactor step and compare against these files to confirm public routes and schemas remain stable.
