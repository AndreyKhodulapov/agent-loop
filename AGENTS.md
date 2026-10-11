# Code requirements

Apply to every project. Where the project's own rules (CLAUDE.md, context file) conflict with these, the project wins.

## Scope

- One task is one commit of at most ~1000 changed lines including tests. If the task does not fit, stop and propose how to split it into separate tasks instead of writing more code.
- Change only what the task needs. Do not reformat neighbouring code or add type hints, renames or cleanups along the way.
- Reuse what the project already has: logger, import style, utilities, fixtures. Search before writing a new helper.
- Follow the idioms of the language and the conventions already used in the repository.

## Design

- Keep the layering: routers -> services -> models. A layer calls only the one below it.
- No abstractions in advance: no interface, factory, config class or parameter without a second real use.
- A module-level global only if the module uses it 3 or more times; otherwise a local variable or a class attribute.

## Errors

- Check only what can actually be wrong. No `is not None` checks for values that are never `None`, no `try/except` around code that does not raise, no fallback defaults nobody needs.
- Never swallow exceptions. Catch a specific exception only where you know what to do with it; otherwise let it propagate.

## Naming

- Concrete domain names, in the project's own vocabulary. Not `data`, `result`, `processed_data`, `handle_request`.
- Short names are fine in a short scope (`i`, `k, v`, `e`).

## Comments and docstrings

- Explain why, not what. Do not restate the code or explain the language to the reader.
- Comments are for the future reader, not the reviewer of this change: no "changed X to Y", "now uses", "fixed".
- No references to plans, task numbers, decisions or paragraphs of planning documents.
- No decorative separators like `# --- public API ---`; a plain `# public API` only where the file is hard to read without it.
- Docstrings for public API and non-trivial logic only, not for small private helpers.
- No markdown emphasis, emoji or exclamations in comments, docstrings and logs; backticks for identifiers are fine. Use `-` instead of an em dash. Avoid filler words: robust, comprehensive, seamlessly, leverage, "This ensures that".

## Tests

- Do not duplicate existing tests; extend or parametrize them.
- Never weaken a test to make it pass: no new `skip`/`xfail`, no removed asserts, no loosened expectations.
- Tests are deterministic: no dependence on execution order, wall-clock time, unseeded randomness or external network.

## Commits

- Message in English: `type(module): gist of changes`. No co-author lines or tool attribution.
