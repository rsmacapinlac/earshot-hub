"""The /v1 HTTP API — the device's one interface for operating it.

`openapi.yaml` is the contract (OpenAPI 3.1; its component schemas are the JSON
Schemas the rest of the code validates against). `validation.py` binds runtime
request/response validation to those schemas.
"""
