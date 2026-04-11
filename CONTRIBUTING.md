# Contributing to Cortex

Thanks for your interest. Cortex is an opinionated system for active
agent memory. The rules below exist to keep the signal-to-noise ratio
high -- please read them before opening a PR.

## Ground rules

### 1. Tripwires must be earned

A tripwire is a lesson from a **specific past failure** with a
quantifiable cost (dollars or hours) and an actionable "how to apply"
section. Before proposing one:

- Point to a specific incident (date, numbers, evidence)
- Write narrow triggers that fire only on relevant prompts
- Include positive AND negative test cases
- Keep the body 3-6 short paragraphs

See [docs/authoring.md](docs/authoring.md) for the full guide including
a "should this be a tripwire" decision tree. If you can't point to a
specific past failure, it probably isn't ready to be a tripwire.

### 2. Rules must be narrow

Rule PRs must include at least one prompt that fires the rule AND one
prompt that doesn't. Test with the CLI before submitting:

```bash
echo '{"prompt":"your test prompt"}' | cortex-hook | python -m json.tool
```

A rule with only `match_any: [fee]` and no `and_any` will fire on
everything. Always add a disambiguating `and_any` set.

### 3. Fail-open is non-negotiable

Every code path reachable from `cortex/hook.py` or `cortex/watch.py`
must exit 0 on any error. A broken Cortex must never block the user's
interaction. This is tested via `test_hook.py::test_hook_invalid_json_fails_open`
and friends -- don't remove those.

### 4. Zero runtime deps beyond stdlib + pyyaml

If you need another runtime dependency, first explain in the PR why
stdlib can't solve it. Cortex runs at hook time on every user prompt;
every import slows down the agent.

Optional tooling (`mempalace` for `cortex import-palace`) is fine as a
soft dependency with graceful degradation.

### 5. Violation patterns: prefer false negatives to false positives

A missed violation is acceptable. A false positive poisons the
effectiveness metric and erodes trust in the injection brief. When in
doubt, leave `violation_patterns` empty -- the tripwire still injects,
it just doesn't contribute to effectiveness measurement.

Test both the bug pattern AND the common fix pattern. The `lookahead_parquet`
regex is a good reference: it catches `slot_ts = (ts // N) * N` but
deliberately passes on `(ts // N) * N + N` (the honest forward-shift fix).

## Development

```bash
git clone https://github.com/jimbokl/LLMCORTEX.git
cd LLMCORTEX
pip install -e ".[dev]"
```

### Running tests

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q
```

The `PYTEST_DISABLE_PLUGIN_AUTOLOAD` env var avoids loading unrelated
third-party pytest plugins that may be installed globally.

### Linting

```bash
ruff check cortex/ tests/
```

## Commit messages

Short and imperative:

- `add synthesizer for cost components`
- `fix regex backtracking in lookahead detection`
- `docs: explain violation_patterns semantics`

## PR checklist

- [ ] Tests pass (`pytest -q`)
- [ ] Ruff passes (`ruff check cortex/ tests/`)
- [ ] If adding a tripwire: positive + negative test added
- [ ] If adding a rule: manual `cortex-hook` test result in PR description
- [ ] If adding a violation pattern: bug pattern + fix pattern both tested
- [ ] README or docs updated if user-visible behavior changed
- [ ] [CHANGELOG.md](CHANGELOG.md) updated under "Unreleased"

## Publishing to PyPI (maintainers only)

Cortex ships as `llmcortex-agent` on PyPI (the name `cortex-agent` is taken
by an unrelated project). The import name stays `cortex`:
`pip install llmcortex-agent` then `from cortex.cli import main`. The
release process:

```bash
# 1. Bump version in pyproject.toml
#    (follow semver: patch for fixes, minor for new features, major for breaking)

# 2. Update CHANGELOG.md
#    Move "Unreleased" changes to a new versioned section with today's date

# 3. Verify tests and build locally
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q
ruff check cortex/ tests/
python -m pip install --upgrade build twine
python -m build                        # produces dist/cortex_agent-X.Y.Z-*

# 4. Verify wheel contents (critical: rules/*.yml must be present)
python -c "
import zipfile, sys
with zipfile.ZipFile('dist/cortex_agent-X.Y.Z-py3-none-any.whl') as z:
    yml = [n for n in z.namelist() if n.endswith('.yml')]
    assert len(yml) >= 2, 'rules/*.yml missing from wheel'
    print('OK:', len(yml), 'YAML files in wheel')
"

# 5. Upload to TestPyPI first (dry-run the release)
python -m twine upload --repository testpypi dist/*
# Verify install from TestPyPI in a clean venv:
python -m venv /tmp/test-cortex && /tmp/test-cortex/bin/pip install \
    --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ \
    llmcortex-agent

# 6. Upload to real PyPI
python -m twine upload dist/*

# 7. Tag the release
git tag vX.Y.Z
git push origin vX.Y.Z

# 8. GitHub release (use `gh release create vX.Y.Z --generate-notes`)
```

PyPI credentials live in `~/.pypirc` or `TWINE_USERNAME` / `TWINE_PASSWORD`
env vars. Use an API token (starts with `pypi-`), never your account
password. Scope the token to the `llmcortex-agent` project only.

## Questions

Open a discussion or file an issue. The best PRs start with an issue
that clarifies scope before code is written.
