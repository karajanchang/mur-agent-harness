# mur agent — Harness Test Run

**Started:** 2026-04-28
**Tester:** Claude (autonomous)
**mur version:** $(mur --version)
**Goal:** Exercise full mur agent feature surface, log every step, fix after 3 accumulated errors.

## Error Counter
- Total errors: 3 / 3 (fix threshold reached — fixes applied)
- Fixed errors: 3 (E1 deployment + E2/E3 source patches)

## Final Status — All 12 Scenarios Pass + Regression Tests Green
- mur-core full test suite: 810 + 829 unit/integration tests, 0 failures
- New regression tests added:
  - `agent_create_parses_provider_slash_model_shorthand`
  - `agent_create_explicit_provider_wins_over_model_prefix`
  - `skill_show_and_remove_accept_basename_and_stem`
- Source patch lives on branch `harness/fix-e1-e2-e3` in `/Users/david/Projects/mur` (worktree at `/tmp/mur-fix-v241`)
- Commit: `9d7ccb3 fix(agent): provider parsing in --model + skill id aliases (E2/E3)`
- E1 deployment: copied built `mur-agent-runtime` to `~/.local/bin/`; existing relative symlinks now resolve.
- Long-term E1 fix should land in the brew formula (`mur-run/homebrew-tap` repo) so future installs ship the runtime alongside `mur`.

## Minor UX observations (not counted as errors)
- `mur agent perm deny-host <glob>` actually *removes from allow list* rather than adding a separate deny rule. Naming suggests a deny entry; consider `perm remove-allow-host` or implement a true deny list.
- `mur agent mcp add ... --arg -y` is rejected by clap (treats `-y` as a flag). Workaround: `--arg=-y`. Could document or use `--arg=`/positional.
- `mur agent card <nonexistent>` says "ephemeral runtime did not produce a card response" — could short-circuit with an "agent not found" check before spawning runtime.



## Errors Found

### E1: `mur-agent-runtime` binary missing (blocks all runtime features)
- **Symptom:** `mur agent create <name>` creates symlink `~/.local/bin/mur_agent_<name> -> mur-agent-runtime` but `mur-agent-runtime` is not in PATH and not present anywhere on disk.
- **Impact:** Cannot start any agent. Blocks S7 (send), S8 (card), S10 (telemetry), and any test that needs a live agent socket.
- **Source:** brew `mur-run/tap/mur` 2.4.1 ships only `/opt/homebrew/Cellar/mur/2.4.1/bin/mur` — no companion runtime.
- **Repro:**
  ```bash
  mur agent create harness_test_1 --no-interactive
  # → Symlink: /Users/david/.local/bin/mur_agent_harness_test_1 -> mur-agent-runtime
  ls -L ~/.local/bin/mur_agent_harness_test_1
  # → No such file or directory
  ```
- **Likely fix locations (mur source):** Either (a) the brew formula must also install `mur-agent-runtime`, or (b) the symlink target should be a subcommand like `mur agent run --profile <dir>` so the existing `mur` binary handles it.
- **Status:** Logged. Continuing with non-runtime tests.

### E3: `mur agent skill add` help text contradicts actual id used by show/remove
- **Symptom:** Help reads "Path to a skill .md file (basename becomes its id)" but the id stored in `profile.yaml` is `skills/<basename>.md`. Calling `mur agent skill show <agent> test_skill` returns "not registered". Only `mur agent skill show <agent> 'skills/test_skill.md'` works.
- **Impact:** UX confusion. Users follow the help and get errors.
- **Repro:**
  ```bash
  mur agent skill add harness_test_1 /tmp/test_skill.md
  mur agent skill list harness_test_1                # → skills/test_skill.md
  mur agent skill show harness_test_1 test_skill     # → Error: skill 'test_skill' not registered
  mur agent skill show harness_test_1 'skills/test_skill.md'  # → works
  ```
- **Likely fix:** Either (a) change show/remove to accept basename match (preferred), or (b) update help text to match reality.
- **Status:** Logged.

### E2: `mur agent create --model` ignores provider, always writes `provider: ollama`
- **Symptom:** Passing `--model claude-opus-4-7` or `--model anthropic/claude-opus-4-7` results in `provider: ollama, name: <whatever>`. There is no CLI flag for provider.
- **Impact:** Silent misconfiguration. Anyone trying to create an Anthropic/OpenAI agent will get an Ollama-typed profile.
- **Repro:**
  ```bash
  mur agent create test --no-interactive --model "anthropic/claude-opus-4-7"
  grep -A2 '^model:' ~/.mur/agents/test/profile.yaml
  # → provider: ollama   name: anthropic/claude-opus-4-7
  ```
- **Likely fix:** Either add `--provider` flag, or parse `provider/model` syntax (`anthropic/claude-opus-4-7` → split). The `mur agent create --help` text should also document accepted formats.
- **Status:** Logged.

## Test Matrix
| # | Scenario | Status | Notes |
|---|----------|--------|-------|
| 1 | Basic lifecycle (create/list/status/remove) | ✅ pass | symlink target initially broken (E1) |
| 2 | Custom display-name + model | ✅ pass after E2 fix | provider hardcoded to ollama before fix |
| 3 | MCP server management | ✅ pass | clap parsing of `-y` needs `--arg=-y` |
| 4 | Skill management | ✅ pass after E3 fix | id mismatch with help text before fix |
| 5 | Permission management | ✅ pass | `deny-host` actually removes from allow |
| 6 | System prompt management | ✅ pass | content is positional arg, not `--content` |
| 7 | A2A message/send | ✅ pass | malformed JSON / missing role return clean errors |
| 8 | Agent card retrieval | ✅ pass | full Agent Card with endpoints + entitlements |
| 9 | Identity rekey + rekey-status | ✅ pass | only specific reasons accepted |
| 10 | Telemetry & logs | ✅ pass | telemetry jsonl populated; stderr.log only via service |
| 11 | Export to .murpkg | ✅ pass | flag is `--out`, not `--output` |
| 12 | Edge cases (dup, invalid names) | ✅ pass | clean errors for dup, dash, space, empty, missing |

---

## Run Log
