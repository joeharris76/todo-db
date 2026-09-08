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
3. Affected versions, runtime environment, and state remote/branch.
4. Any proposed mitigations.

We will acknowledge receipt within 72 hours and provide status updates as triage and fixes proceed.

## Security Scope

Security-critical areas in `todo-db` include:
- **State-branch confidentiality**: the state branch shares its repository's
  access and visibility. Never publish credentials, tokens, or connection
  strings to it, and never assume a separate branch is private.
- **Claim integrity**: ownership plus generation checks protect renew,
  release, and finish from stale writers; adoption rotates the generation
  so a duplicated worker identity fails closed.
- **MCP server trust boundaries**: client principals are explicitly tracked
  (one server instance is one worker identity) and never treated as access
  control against a malicious repository writer.
- **Symlink and path safety**: state checkouts never follow symlinks, and
  task IDs cannot escape the `items/` directory.
