# Security Policy

## Supported Versions

`todo-db` supports security fixes on:

| Version | Supported |
| ------- | --------- |
| 0.6.x   | Yes       |
| < 0.6.0 | No        |

## Reporting a Vulnerability

Do not open public GitHub issues for suspected security vulnerabilities.

Please report vulnerabilities via [GitHub Private Vulnerability Reporting](https://github.com/joeharris76/todo-db/security/advisories/new) or by email to `security@joeharris.dev`.

Please include:
1. Description of the vulnerability and potential security impact.
2. Reproduction steps, code snippet, or proof of concept.
3. Affected versions, runtime environment, and storage backend (local SQLite or hosted Turso/libSQL).
4. Any proposed mitigations.

We will acknowledge receipt within 72 hours and provide status updates as triage and fixes proceed.

## Security Scope

Security-critical areas in `todo-db` include:
- **Credential isolation and resolution**: Ensuring credentials resolved via `TODO_DB_CREDENTIAL_COMMAND` or environment variables never leak into logs, doctor output, subprocess environments, or tracker evidence.
- **Audit chain integrity**: Ensuring the hash-chained audit envelope (`sha256-chain-v2`) detects tampering or history rewrite.
- **MCP server trust boundaries**: Ensuring client principals are explicitly tracked and capability-scoped credentials are not reused across distinct actor boundaries.
- **Subprocess execution**: Safeguards in `verify-run` (including `TODO_DB_ALLOW_HOSTED_VERIFY_RUN` requirements and environment allowlisting) preventing lateral code execution channels.
