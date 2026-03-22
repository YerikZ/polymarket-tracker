# polymarket-tracker — Claude instructions

## Git commits

After completing any of the following, **always create a git commit** before finishing your response:
- Adding a new feature
- Fixing a bug
- Refactoring existing code

Use a concise, descriptive commit message that reflects the change (e.g. `fix: closed-market signals leaking through stream path` or `feat: wallet scoring system`).

**Do not commit** for:
- Exploratory file reads or searches
- Answering questions / explaining code
- Partial work mid-task (wait until the task is complete)

Stage only relevant files (`git add -u` plus any new source files). Never commit `config.yaml`.
