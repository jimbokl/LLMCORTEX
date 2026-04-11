"""Verifiers: code-level pre-flight checks tied to tripwires.

Each verifier is a standalone script that returns exit 0 (pass) or non-zero
(fail) and can optionally emit a JSON diagnostic for hook consumption.
"""
