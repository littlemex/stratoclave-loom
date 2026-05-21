# Contributing to stratoclave-loom

Thanks for your interest in improving stratoclave-loom. This document describes
how to set up a development environment, the expectations we hold around
code quality, and the workflow for submitting changes.

By participating you agree to uphold our
[Code of Conduct](./CODE_OF_CONDUCT.md).

> **Note:** stratoclave-loom is in alpha. The public Python API, CLI flags,
> and ACP wire format may change between commits. If you are planning
> non-trivial work, please open an issue to discuss the approach first.

## Table of Contents

- [Ways to contribute](#ways-to-contribute)
- [Reporting bugs](#reporting-bugs)
- [Proposing features](#proposing-features)
- [Development setup](#development-setup)
- [Testing](#testing)
- [Coding style](#coding-style)
- [Commit messages](#commit-messages)
- [Pull requests](#pull-requests)
- [Security issues](#security-issues)

## Ways to contribute

- **Bug reports** with clear reproduction steps.
- **Feature proposals** that articulate the problem first, solution second.
- **Documentation improvements** — typo fixes, clearer examples, translations.
- **Adapters for new agent backends** (OpenCode, Kiro, Codex, …).
- **Reviews** of open pull requests from other contributors.

## Reporting bugs

Use the **Bug report** issue template. Include:

- What you expected to happen vs. what actually happened.
- Minimal reproduction steps.
- Environment: commit SHA or release tag, OS, Python version, agent backend
  name and version.
- Redacted logs if they help. **Remove secrets** (API keys, OAuth tokens).

Do not report suspected vulnerabilities in public issues — see
[Security issues](#security-issues).

## Proposing features

Use the **Feature request** issue template. Keep the focus on:

1. The problem you're trying to solve, for whom.
2. Your proposed approach.
3. Alternatives you considered and why you discarded them.

We favour small, composable changes. Large features usually require a design
discussion in an issue before a PR is reviewed. New adapters should follow
the contract defined in `src/stratoclave_loom/core/backend.py`.

## Development setup

### Prerequisites

- **Python 3.11+**
- `pip` (or `uv` if you prefer)
- An installed agent CLI for end-to-end testing (Claude Code is the
  reference adapter for v0.1)

### Fork and clone

```bash
git clone https://github.com/<your-username>/stratoclave-loom.git
cd stratoclave-loom
git remote add upstream https://github.com/littlemex/stratoclave-loom.git
```

Work on feature branches created from `main`:

```bash
git checkout -b feat/descriptive-name
```

### Install in editable mode

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

This installs the library plus the development extras (`pytest`, `ruff`,
`mypy`).

## Testing

We expect PRs to be accompanied by tests where reasonable:

- Unit tests for pure logic (`tests/unit/`).
- Adapter contract tests run against a mock backend (`tests/adapters/`).
- End-to-end tests against a real agent CLI live under `tests/e2e/` and are
  skipped by default — enable them with `pytest -m e2e` once the relevant
  CLI is installed.

Run the suite:

```bash
pytest
```

If a test harness is missing for the area you're touching, add one or note
the gap in the PR description.

## Coding style

Formatters and linters are authoritative — run them before pushing.

| Tool   | Command                  |
|--------|--------------------------|
| Format | `ruff format .`          |
| Lint   | `ruff check .`           |
| Types  | `mypy src/ tests/`       |

Other expectations:

- Prefer small, focused modules over large omnibus files.
- Avoid introducing new runtime dependencies without justifying them.
- Public APIs (functions exported from `stratoclave_loom`) must have
  docstrings.
- **Do not hard-code paths, URLs, model names, or any environment-specific
  values.** Use environment variables (see `BackendConfig`) or function
  arguments. This is a project-wide rule.

## Commit messages

We use **[Conventional Commits](https://www.conventionalcommits.org/)**:

```
<type>(<scope>): <short summary>
```

Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `build`,
`ci`, `perf`. Example:

```
feat(adapters): add OpenCode backend
fix(transport): drain stdout on cancel before SIGTERM
```

Keep commits atomic and write meaningful bodies for non-trivial changes.
Reference issues with `Refs #N` or `Closes #N`.

## Pull requests

1. Rebase on the latest `main` before opening the PR.
2. Fill in the pull-request template completely.
3. Keep PRs focused. If a change grows, split it.
4. Ensure CI is green (formatters, linters, type checks, unit tests).
5. Request review from a maintainer. We typically respond within a week.
6. Address review feedback with additional commits; we squash on merge.

We do not require Contributor License Agreements (CLAs); the Apache-2.0
license covers contributions.

## Security issues

Do **not** report suspected vulnerabilities in public issues or pull
requests. Follow the process in [`SECURITY.md`](./SECURITY.md).

---

If you have questions before filing an issue or PR, feel free to reach out
via GitHub Discussions (once enabled) or an issue tagged `question`.
