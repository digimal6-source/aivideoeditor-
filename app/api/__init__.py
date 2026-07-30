"""HTTP API layer.

The API is intentionally decoupled from the processing code: it only speaks to
:class:`app.api.service.AppService`. Swapping the stdlib server for ASGI, or
moving the worker onto a separate machine, means replacing this package only.
"""
