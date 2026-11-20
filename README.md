# AGENTS.md A/B tester

Measures the effect of an `AGENTS.md` file on Claude Code. It runs two Claude Code agents side by side in isolated Docker containers and sends them the same prompts:

| Agent | Workspace |
|---|---|
| **A** | the project (or an empty folder) **plus the AGENTS.md under test** |
| **B** (baseline) | the same project, **without** AGENTS.md |

Both containers use the same image, Claude Code version, model and prompts. The only difference between them is the AGENTS.md file.

## Requirements

- Docker (Docker Desktop on Windows/macOS) running
- Python 3.12+ (standard library only)
- Credentials in the git-ignored `.env/` folder (see below)

## Credentials (`.env/`)

Use one of these:

1. **Recommended: a long-lived token.** Run `claude setup-token` on your machine, then create `.env/claude.env`:
   ```
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
   ```
   You can also use `ANTHROPIC_API_KEY=...`. The file is passed to both containers with `docker run --env-file`. When it defines a token, `.env/.credentials.json` is ignored.
2. **`.env/.credentials.json`**: a copy of `~/.claude/.credentials.json` taken after `claude login`.

   OAuth refresh tokens can be used only once. The script refreshes the token once before the agents start, keeps the newest credentials in sync between the containers, and writes them back to `.env/.credentials.json`.

   If this file was copied from your own `~/.claude`, the token rotation can log out the Claude Code install on your machine, or the reverse can happen. Option 1 avoids that.

`.env/`, `*.env` and `runs/` are listed in `.gitignore`.

## Usage

```bash
python agentsmd_tester.py                     # fully interactive
python agentsmd_tester.py --agents-md path/to/AGENTS.md --prompt-file task.md          # start from scratch
python agentsmd_tester.py --agents-md path/to/AGENTS.md --project ../my-app --model opus # edit an existing project
```

### Example

[inputs/](inputs/) has sample inputs: a secure-coding [AGENTS.md](inputs/AGENTS.md) and a development prompt.

```bash
python agentsmd_tester.py --mode new --agents-md inputs/AGENTS.md --prompt-file inputs/1.develop.md
# when both agents finish: menu option 1, then type your evaluation prompt (or @path/to/prompt.md)
```

The script:

1. Asks for the use case if it isn't given:
   - **new**: empty workspace
   - **existing**: copies `--project` into both workspaces
2. Asks for the AGENTS.md under test and the initial prompt. Prompts are multi-line: end with a line containing only `.`, or type `@file.md` to load one from a file.
3. Builds the `agentsmd-tester` image from [docker/Dockerfile](docker/Dockerfile) and starts two containers, each on its own Docker network.
4. Copies the workspaces in and sends the prompt to both agents in parallel. Each agent runs `claude -p --dangerously-skip-permissions` in `/workspace`, and both agents' activity is streamed live.
5. Opens a menu where you can:
   - send follow-up prompts (evaluation questions, for example) to both agents or to one of them. Each agent continues its own conversation through `--resume`.
   - copy both workspaces to the host for manual inspection
   - view a summary of time, agent turns, tool calls, tokens and cost
6. Removes the containers when you quit. Unless the current workspaces were already copied, it first offers to copy them.

Press Ctrl+C during a round to stop both agents. Their sessions are kept, so you can send another prompt afterwards.

## Output (`runs/<timestamp>/`)

```
AGENTS.under-test.md        copy of the tested file
prompts.md                  every prompt sent, per round
run.json / summary.md       per-turn metrics and final responses
agent_a/ agent_b/
  transcript.md             readable transcript (prompts, tool calls, responses)
  turn_NN.jsonl             raw Claude Code stream-json events
workspaces/after_round_NN/
  agent_a_with_agents_md/
  agent_b_without_agents_md/
```

## How AGENTS.md reaches agent A

Recent Claude Code versions read `AGENTS.md` natively when a project has **no** `CLAUDE.md`. So agent A only gets `AGENTS.md` in its workspace root, which matches how it is used in practice.

When copying an existing project, the script takes out every `AGENTS.md`, `CLAUDE.md` and `CLAUDE.local.md` file from **both** copies, so neither agent sees other instructions. With `--keep-instruction-files`, those files are kept in both copies. The root AGENTS.md is still replaced for A and removed for B. A kept root CLAUDE.md would stop Claude Code from loading AGENTS.md, so in that case `@AGENTS.md` is added to A's CLAUDE.md. `--claude-md-bridge always|never` overrides this.

If the project is a git repository and those files are committed, an agent could still find them through git. The script warns you when this applies.

## Useful options

| Option | Purpose |
|---|---|
| `--model opus` | pin the model for both agents (recommended for reproducibility) |
| `--claude-version 2.1.329` | pin the Claude Code version in the image |
| `--timeout 30` | per-prompt timeout in minutes |
| `--max-turns N` | limit agentic turns per prompt |
| `--exclude NAME` | extra file/dir name not copied into the workspaces (`node_modules`, `.venv`, `__pycache__`, ... are always skipped) |
| `--memory 4g --cpus 2` | resource limits per container |
| `--keep-containers` | keep the containers after exit (`docker exec -it <name> bash`) |
| `--quiet` | don't stream live activity |

Add any toolchains your projects need (Go, Rust, Java, ...) to [docker/Dockerfile](docker/Dockerfile). Both agents get them.
