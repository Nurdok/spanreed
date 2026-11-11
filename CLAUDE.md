# Spanreed

## Pull requests

Always open a pull request for changes you make in this repository. After
committing and pushing your work to the feature branch, create a PR against
`main` without waiting to be asked. If the branch already has an open PR, push
to it instead of opening a new one.

## Checks

Run these before pushing:

- `poetry run pytest` (tests live next to the code as `*_test.py`)
- `poetry run black --check src`
- `poetry run mypy src`
