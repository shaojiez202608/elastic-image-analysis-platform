# Standards review

Review of the repository against appendix A-F of the project standards (prototype rules and lessons learned).
Status values: 满足 (met), 需修改 (needs change - none left open), 不适用 (not applicable, with reason).
Evidence is `file:line` at tag `v0.1.0`.

## A. 定性与脱敏

| Item | Status | Evidence |
|---|---|---|
| README first screen: non-production, user responsible, security/legal review, link to hardening checklist | 满足 | README.md:3-7; README.zh-CN.md:3-5; docs/handover.md "Production hardening checklist" |
| grep for customer names, people, domains, 12-digit numbers, profile names, template leftovers = 0 | 满足 | tests/test_repo_hygiene.py (account IDs, local paths); final self-check in VERIFICATION.md "Repository self-check" |
| Placeholders `<UPPER_SNAKE>`, `example.com`, `111122223333`; screenshots sourced; synthetic data | 满足 | .github/workflows/deploy-verify.yml:31; scripts/cloudlib.py `redact`; sample-data/README.md; no screenshots (SVG timelines are generated, provenance in VERIFICATION.md) |

## B. 仓库标配

| Item | Status | Evidence |
|---|---|---|
| LICENSE (MIT-0 + copyright), SECURITY (full form), CoC, CONTRIBUTING (tests, commits, no account IDs) | 满足 | LICENSE:3; SECURITY.md; CODE_OF_CONDUCT.md; CONTRIBUTING.md "Tests are required", "Rules" |
| THIRD-PARTY-LICENSES two tables; CHANGELOG (Keep a Changelog) + VERSION | 满足 | THIRD-PARTY-LICENSES sections 1 and 2; CHANGELOG.md; VERSION |
| Issue/PR templates, AGENTS.md + CLAUDE.md, Makefile, .gitignore/.dockerignore | 满足 | .github/ISSUE_TEMPLATE/; .github/pull_request_template.md; AGENTS.md; CLAUDE.md:1; Makefile; .gitignore; .dockerignore |
| DECISIONS / OPEN-QUESTIONS / SECURITY-NOTES / THREAT_MODEL (DRAFT) / VERIFICATION / DEPLOYMENT-LOG | 满足 | repository root; THREAT_MODEL.md:3 "Status: DRAFT" |

## C. 配置与部署

| Item | Status | Evidence |
|---|---|---|
| Config as templates, input validation, removal policy as parameter, unified tags | 满足 | config/*.example.json; infra/imaging_infra/config.py:170-245 (validation), :183 (removal_policy), :29 (required tags); .gitignore (config/*.json) |
| Preflight (account, Region, toolchain, quotas), destructive-change guard, end-to-end verification | 满足 | scripts/preflight.sh:25 (account confirmation), :42 (endpoint quota), :54 (Fargate ARM64 zone), "4. destructive-change guard"; scripts/check_destructive.py; scripts/e2e_cloud.py |
| Verification records with date, Region, configuration, commit, failures, limits | 满足 | VERIFICATION.md; DEPLOYMENT-LOG.md |

## D. 安全与 CI

| Item | Status | Evidence |
|---|---|---|
| Static checks clean, every acknowledgement reasoned; trust policies with SourceAccount + SourceArn; resource policies on sensitive resources | 满足 | infra/imaging_infra/nag.py; SECURITY-NOTES.md "cdk-nag acknowledgements"; infra/imaging_infra/trust.py:62; API resource policy and domain access policy (search_stack.py); key-policy conditions (common.py `harden_key_policies`) |
| CI with lint, tests, synth, secret scan; Actions pinned by SHA, minimal permissions; Dependabot groups; gitleaks exemptions reasoned | 满足 | .github/workflows/ci.yml:7 (permissions), :22-26 (SHA pins from git ls-remote); .github/dependabot.yml; .gitleaks.toml (no exemptions) |
| No account-level settings changed | 满足 | search_stack.py `cloud_watch_role=False`; tests/test_infra.py `test_api_methods_use_iam_and_resource_policy` (no AWS::ApiGateway::Account); preflight only reads service-linked roles and quotas |

## E. 清理

| Item | Status | Evidence |
|---|---|---|
| Clean-up script with confirmations and a residual check; README lists what is not removed automatically | 满足 | scripts/destroy.sh:23-25; scripts/check_residuals.py:238; README.md "Clean-up"; docs/cleanup.md |
| All used Regions checked, explicit conclusion | 满足 | DEPLOYMENT-LOG.md "Residual check" (us-west-2 deployed; us-east-1 checked as well) |

## F. 文档与提交

| Item | Status | Evidence |
|---|---|---|
| Fact-check table with official links and dates; billing as dimensions only; README section order | 满足 | docs/architecture.md "Fact check"; README.md "Prerequisites and billing dimensions" (no figures, no money wording - DECISIONS 13); README order: disclaimer -> architecture -> prerequisites -> deployment -> usage -> troubleshooting -> security -> clean-up -> layout -> local testing -> contributing/security/license |
| Conventional Commits with verification in the body; not pushed | 满足 | `git log`; VERIFICATION.md "Repository self-check" |

## Not applicable

| Item | Reason |
|---|---|
| AgentCore checklist | the project uses no AgentCore components |
| THREAT_MODEL "prompt injection" focus | no LLM processes untrusted input; the model is an image classifier whose output is still validated as untrusted (THREAT_MODEL T6) |

Summary: 满足 all appendix items (A 3, B 4, C 3, D 3, E 2, F 2); 不适用 2 (listed above); 需修改 0.
