"""PBIP ML/analytics package.

Rules (see AGENTS.md section 9):
* ``contract`` modules define the interfaces the API depends on. Changing them is a
  cross-team decision and needs an ADR.
* Request-path code (inference) must be fast, deterministic and free of training imports.
* Training code is never imported by the API.
"""

__version__ = "0.1.0"
