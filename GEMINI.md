# Project Guidelines: Anime Discovery

## Core Engineering Principles

### 1. KISS Principle (Keep It Simple, Stupid)
- Favor simple, direct, and explicit implementations over clever or convoluted designs.
- Avoid premature abstractions, speculative extensibility, or unnecessary configuration layers.
- Write readable code with clear control flow and minimal indirection.
- Solve the current requirement completely without over-engineering for hypothetical future requirements.

### 2. Code Reuse
- Always inspect existing modules in `src/nextgen/` before authoring new utilities, models, helpers, or contracts.
- Reuse established project patterns, services, cleanup routines, job handlers, and database contracts.
- Reuse test fixtures, factories, and helpers from `src/nextgen/tests/` rather than reinventing test setups.
- Eliminate duplicate or parallel implementations across the codebase.

### 3. Avoid Excessive Complexity
- Prefer flat, cohesive module structures and standalone functions over deep inheritance hierarchies or nested abstractions.
- Keep function and class interfaces minimal, focused, and purposeful.
- Strictly adhere to NextGen architectural boundaries: do not introduce transitional data assemblers, legacy compatibility bridges, or complex shim layers.

---

## Architecture & Project Boundaries

- **NextGen Focus**: Active development belongs exclusively in `src/nextgen/`. Legacy code in `src/legacy/` is reference-only for domain rules and regression scenarios.
- **Database & Schemas**: Follow existing SQLite migration and schema patterns (`src/nextgen/`). Keep schema changes clean, immutable, and explicit.
- **Testing**: Maintain high test reliability with `pytest`. Ensure tests run deterministically and exercise failure/cleanup paths.

---

## Operational Guardrails & Git Rules

- **Branching & PR Workflow**: Always create a new branch before starting work. Run batch tests locally (`PYTHONPATH=. pytest src/nextgen/tests/`) and fix issues as they appear. Create a PR, commit, and push.
- **CI Runs**: Always include `[skip ci]` in commit messages and PR descriptions to avoid triggering automated CI workflows.
- **Safety**: Never deploy, restart production services, alter production databases, or mutate/delete user media or torrents without explicit authorization.
