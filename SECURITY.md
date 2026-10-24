# Security policy

## Supported version

Security fixes are applied to the latest release on the default branch.

## Reporting a vulnerability

Do not open a public issue for a vulnerability or suspected credential leak. Use GitHub's private vulnerability reporting for this repository. Include the affected version, reproduction steps, and the possible impact.

## Secrets and organizational data

Never commit Slack tokens, signing secrets, OAuth credentials, AWS credentials, customer data, internal channel IDs, or production resource identifiers. Store runtime credentials in AWS Systems Manager Parameter Store as `SecureString` values.

The repository contains synthetic examples only. Before sharing logs or fixtures in an issue, replace names, message text, URLs, IDs, and timestamps with synthetic values.

## Dependency audit

CI runs `npm audit --omit=dev --audit-level=high` and blocks high-severity vulnerabilities in production dependencies.

As of 2026-08-13, the latest AWS CDK library contains a bundled development-only dependency (`brace-expansion@5.0.8`) covered by GHSA-rgw5-rvv9-x895. It is used while synthesizing infrastructure and is not included in the deployed Lambda or AgentCore application code. Because it is bundled by `aws-cdk-lib`, npm overrides cannot replace it. The project will update AWS CDK when an upstream release includes the fixed dependency.
