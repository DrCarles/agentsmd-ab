#!/usr/bin/env python3
"""A/B test the effect of an AGENTS.md file on Claude Code agents.

Two Claude Code agents run side by side, each in its own Docker container:

  * Agent A gets the AGENTS.md under test in its workspace.
  * Agent B (baseline) gets the exact same workspace without it.

Both receive the same prompt. Afterwards you can send follow-up prompts
(typically evaluation questions) to both agents or to one of them, then copy
both workspaces to the host for manual inspection.

Credentials are read from the git-ignored `.env/` folder:
  .env/.credentials.json   Claude Code OAuth credentials (copied into ~/.claude/)
  .env/claude.env          optional KEY=VALUE env file (ANTHROPIC_API_KEY,
                           CLAUDE_CODE_OAUTH_TOKEN, ...) passed to the containers

Only the Python standard library and the Docker CLI are required.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCKER_DIR = ROOT / "docker"
SECRETS_DIR = ROOT / ".env"
CREDENTIALS_FILE = SECRETS_DIR / ".credentials.json"
ENV_FILE = SECRETS_DIR / "claude.env"

IMAGE = "agentsmd-tester:latest"
WORKSPACE = "/workspace"
AGENT_UID = 1000  # the `node` user of the node:* base image

# Instruction files Claude Code (or other agents) may pick up automatically.
INSTRUCTION_FILES = {"agents.md", "claude.md", "claude.local.md"}
# Never copied into the containers (host-specific / huge / platform-specific).
DEFAULT_COPY_IN_EXCLUDES = {"node_modules", ".venv", "venv", "__pycache__",
                            ".pytest_cache", ".mypy_cache", ".tox"}
# Skipped by default when copying workspaces back to the host.
HEAVY_DIRS = ["node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
              ".mypy_cache", ".tox", ".cache", ".next", "target"]

PROMPT_END = "."


# --------------------------------------------------------------------------- #
# Console helpers
# --------------------------------------------------------------------------- #
class Console:
    COLORS = {"a": "\033[36m", "b": "\033[35m", "warn": "\033[33m",
              "err": "\033[31m", "ok": "\033[32m", "dim": "\033[2m",
              "bold": "\033[1m"}
    RESET = "\033[0m"

    def __init__(self, color: bool):
        self.color = color
        self.lock = threading.Lock()
        if color and os.name == "nt":
            os.system("")  # enables ANSI escape sequences in the Windows console
        try:
            sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    def c(self, key: str, text: str) -> str:
        return f"{self.COLORS[key]}{text}{self.RESET}" if self.color else text

    def print(self, text: str = "", style: str | None = None) -> None:
        with self.lock:
            print(self.c(style, text) if style else text, flush=True)

    def agent(self, agent: "Agent", text: str) -> None:
        tag = self.c(agent.key, f"[{agent.tag}]")
        with self.lock:
            for line in text.splitlines() or [""]:
                print(f"{tag} {line}", flush=True)

    def info(self, text: str) -> None:
        self.print(text, "ok")

    def warn(self, text: str) -> None:
        self.print(f"WARNING: {text}", "warn")

    def error(self, text: str) -> None:
        self.print(f"ERROR: {text}", "err")

    def header(self, text: str, style: str = "bold") -> None:
        width = max(60, min(100, len(text) + 8))
        self.print("\n" + f" {text} ".center(width, "="), style)


def ask(question: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{question}{suffix}: ").strip().strip('"').strip("'")
    return answer or (default or "")


def ask_yes_no(question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{question} [{hint}]: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def read_prompt(console: Console, title: str) -> str:
    console.print(f"\n{title}", "bold")
    console.print(f"Type or paste the prompt, then finish with a line containing only "
                  f"'{PROMPT_END}'.\nOr type @path/to/file to load the prompt from a file.",
                  "dim")
    lines: list[str] = []
    while True:
        line = input()
        if not lines and line.startswith("@"):
            path = Path(line[1:].strip().strip('"'))
            if path.is_file():
                text = path.read_text(encoding="utf-8").strip()
                console.print(f"Loaded {len(text)} characters from {path}", "dim")
                return text
            console.warn(f"File not found: {path} (treating the line as prompt text)")
        if line.strip() == PROMPT_END:
            break
        lines.append(line)
    return "\n".join(lines).strip()


def one_line(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def fmt_secs(ms: float | None) -> str:
    if ms is None:
        return "-"
    s = ms / 1000
    return f"{s:.0f}s" if s < 120 else f"{s / 60:.1f}m"


def fmt_cost(cost: float | None) -> str:
    return "-" if cost is None else f"${cost:.4f}"


# --------------------------------------------------------------------------- #
# Docker helpers
# --------------------------------------------------------------------------- #
class DockerError(RuntimeError):
    pass


def docker(*args: str, input: bytes | None = None, check: bool = True,
           stream: bool = False) -> subprocess.CompletedProcess:
    cmd = ["docker", *args]
    if stream:
        res = subprocess.run(cmd, input=input)
    else:
        res = subprocess.run(cmd, input=input, capture_output=True)
    if check and res.returncode != 0:
        err = (res.stderr or b"").decode("utf-8", "replace").strip() if not stream else ""
        raise DockerError(f"`docker {' '.join(args[:3])} ...` failed: {err}")
    return res


def docker_out(*args: str) -> str:
    return docker(*args).stdout.decode("utf-8", "replace").strip()


def check_docker() -> None:
    if not shutil.which("docker"):
        raise DockerError("The docker CLI was not found on PATH.")
    res = docker("info", "--format", "{{.OSType}}", check=False)
    if res.returncode != 0:
        raise DockerError("Cannot reach the Docker daemon. Is Docker Desktop running?")


def build_image(console: Console, claude_version: str, rebuild: bool) -> str:
    console.header("Building agent image")
    args = ["build", "-t", IMAGE, "--build-arg", f"CLAUDE_CODE_VERSION={claude_version}"]
    if rebuild:
        args.append("--no-cache")
    res = docker(*args, str(DOCKER_DIR), stream=True, check=False)
    if res.returncode != 0:
        raise DockerError("docker build failed (see output above).")
    version = docker_out("run", "--rm", IMAGE, "claude", "--version")
    console.info(f"Image {IMAGE} ready - Claude Code {version}")
    return version


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #
@dataclass
class Agent:
    key: str            # "a" or "b"
    tag: str            # short label shown in the live output
    title: str          # long label
    folder: str         # folder name used when copying the workspace out
    container: str
    network: str
    log_dir: Path
    session_id: str | None = None
    turns: list[dict] = field(default_factory=list)
    stop_requested: bool = False

    def short_status(self) -> str:
        return self.turns[-1]["status"] if self.turns else "idle"


def start_container(agent: Agent, run_id: str, args: argparse.Namespace) -> None:
    docker("network", "create", "--label", f"agentsmd-tester={run_id}", agent.network)
    cmd = ["run", "-d", "--name", agent.container, "--hostname", f"agent-{agent.key}",
           "--network", agent.network, "--label", f"agentsmd-tester={run_id}",
           "--init"]
    if ENV_FILE.is_file():
        cmd += ["--env-file", str(ENV_FILE)]
    if args.memory:
        cmd += ["--memory", args.memory]
    if args.cpus:
        cmd += ["--cpus", args.cpus]
    docker(*cmd, IMAGE, "sleep", "infinity")


def exec_with_input(container: str, shell_cmd: str, data: bytes) -> None:
    docker("exec", "-i", container, "sh", "-c", shell_cmd, input=data)


WRITE_CREDENTIALS_SH = "mkdir -p ~/.claude && umask 077 && cat > ~/.claude/.credentials.json"
TOKEN_ENV_VARS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN",
                  "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")


def env_file_provides_auth() -> bool:
    if not ENV_FILE.is_file():
        return False
    keys = {line.split("=", 1)[0].strip() for line in ENV_FILE.read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.lstrip().startswith("#")}
    return any(k in keys for k in TOKEN_ENV_VARS)


def uses_credentials_file() -> bool:
    return CREDENTIALS_FILE.is_file() and not env_file_provides_auth()


def credentials_expiry_ms(raw: bytes | None) -> int:
    try:
        return int(json.loads(raw)["claudeAiOauth"]["expiresAt"])  # type: ignore[arg-type]
    except Exception:
        return 0


def read_container_credentials(agent: Agent) -> bytes | None:
    res = docker("exec", agent.container, "cat", "/home/node/.claude/.credentials.json", check=False)
    return res.stdout if res.returncode == 0 and res.stdout.strip() else None


def refresh_credentials(console: Console) -> bool:
    """OAuth refresh tokens are single-use: if both agents refreshed the same expired token,
    one of them would be logged out. So refresh once, in a throwaway container, up front."""
    raw = CREDENTIALS_FILE.read_bytes()
    if credentials_expiry_ms(raw) > (time.time() + 10 * 60) * 1000:
        return True
    console.print("OAuth access token expired or about to expire - refreshing it once ...", "dim")
    script = (f"{WRITE_CREDENTIALS_SH} && echo '{{\"hasCompletedOnboarding\":true}}' > ~/.claude.json"
              " && claude -p 'Reply with OK' --model haiku --max-turns 1 </dev/null >/dev/null 2>&1;"
              " cat ~/.claude/.credentials.json")
    res = docker("run", "--rm", "-i", IMAGE, "sh", "-c", script, input=raw, check=False)
    if res.returncode == 0 and credentials_expiry_ms(res.stdout) > credentials_expiry_ms(raw):
        CREDENTIALS_FILE.write_bytes(res.stdout)
        console.info("Token refreshed (and saved back to .env/.credentials.json).")
        return True
    console.error("Could not refresh the OAuth token in .env/.credentials.json (its refresh token "
                  "is no longer valid). Either put a long-lived token in .env/claude.env "
                  "(CLAUDE_CODE_OAUTH_TOKEN=... from `claude setup-token`, recommended) or log in "
                  "again and copy a fresh ~/.claude/.credentials.json into .env/ (see README).")
    return False


def sync_credentials(agents: list[Agent]) -> None:
    """Propagate the most recent OAuth credentials to every container and to the host file,
    since a refresh inside one container invalidates the refresh token everywhere else."""
    if not uses_credentials_file():
        return
    sources: list[tuple[str, bytes | None]] = [("host", CREDENTIALS_FILE.read_bytes())]
    sources += [(a.key, read_container_credentials(a)) for a in agents]
    best = max((raw for _, raw in sources if raw), key=credentials_expiry_ms, default=None)
    if best is None:
        return
    for where, raw in sources:
        if raw == best or (raw is None and where != "host"):  # None: container not running
            continue
        if where == "host":
            CREDENTIALS_FILE.write_bytes(best)
        else:
            exec_with_input(next(a for a in agents if a.key == where).container,
                            WRITE_CREDENTIALS_SH, best)


def install_credentials(agent: Agent) -> None:
    if uses_credentials_file():
        exec_with_input(agent.container, WRITE_CREDENTIALS_SH, CREDENTIALS_FILE.read_bytes())
    settings = {"hasCompletedOnboarding": True, "bypassPermissionsModeAccepted": True}
    exec_with_input(agent.container, "cat > ~/.claude.json", json.dumps(settings).encode())


def remove_containers(agents: list[Agent]) -> None:
    for a in agents:
        docker("rm", "-f", a.container, check=False)
        docker("network", "rm", a.network, check=False)


def stop_agent_process(agent: Agent) -> None:
    """Kill the claude process inside the container (killing `docker exec` is not enough)."""
    agent.stop_requested = True
    docker("exec", agent.container, "pkill", "-TERM", "-f", "claude -p", check=False)


# --------------------------------------------------------------------------- #
# Workspace transfer
# --------------------------------------------------------------------------- #
def _looks_executable(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def _as_agent_user(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = AGENT_UID
    info.uname = info.gname = "node"
    if info.isdir():
        info.mode = 0o755
    elif info.isfile():
        info.mode = 0o755 if info.mode & 0o111 else 0o644
    return info


def build_workspace_tar(tar_path: Path, project: Path | None, extra_files: dict[str, bytes],
                        excludes: set[str], strip_instructions: bool) -> list[str]:
    """Write the workspace content to a tar file. Returns the instruction files left out."""
    removed: list[str] = []
    with tarfile.open(tar_path, "w") as tar:
        if project:
            for dirpath, dirnames, filenames in os.walk(project):
                rel_dir = Path(dirpath).relative_to(project)
                dirnames[:] = sorted(d for d in dirnames if d not in excludes)
                for d in dirnames:
                    full = Path(dirpath) / d
                    tar.add(full, arcname=(rel_dir / d).as_posix(), recursive=False,
                            filter=_as_agent_user)
                for f in sorted(filenames):
                    rel = (rel_dir / f).as_posix()
                    if f in excludes or rel in extra_files:
                        continue
                    if f.lower() in INSTRUCTION_FILES and (strip_instructions or rel.lower() == "agents.md"):
                        removed.append(rel)
                        continue
                    full = Path(dirpath) / f
                    info = tar.gettarinfo(full, arcname=rel)
                    if info.isfile() and _looks_executable(full):
                        info.mode |= 0o111
                    info = _as_agent_user(info)
                    if info.isfile():
                        with open(full, "rb") as fh:
                            tar.addfile(info, fh)
                    else:
                        tar.addfile(info)
        for rel, data in extra_files.items():
            info = _as_agent_user(tarfile.TarInfo(rel))
            info.size = len(data)
            info.mtime = int(time.time())
            tar.addfile(info, fileobj=io.BytesIO(data))
    return removed


def upload_workspace(agent: Agent, tar_path: Path) -> None:
    with open(tar_path, "rb") as fh:
        res = subprocess.run(["docker", "exec", "-i", agent.container,
                              "tar", "-xf", "-", "-C", WORKSPACE, "--no-same-owner"],
                             stdin=fh, capture_output=True)
    if res.returncode != 0:
        raise DockerError(f"Copying the workspace into {agent.container} failed: "
                          f"{res.stderr.decode('utf-8', 'replace')}")


def export_workspace(agent: Agent, dest: Path, skip_heavy: bool) -> tuple[int, list[str]]:
    """Stream /workspace out of the container as a tar and extract it on the host."""
    dest.mkdir(parents=True, exist_ok=True)
    cmd = ["docker", "exec", agent.container, "tar", "-C", WORKSPACE, "-cf", "-"]
    if skip_heavy:
        cmd += [f"--exclude={d}" for d in HEAVY_DIRS]
    cmd.append(".")
    count, skipped = 0, []
    with tempfile.TemporaryFile() as err, \
            subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err) as proc:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                try:
                    tar.extract(member, dest, filter="data")
                    if member.isfile():
                        count += 1
                except (OSError, tarfile.TarError) as exc:
                    skipped.append(f"{member.name} ({exc.__class__.__name__}: {exc})")
        proc.wait()
        if proc.returncode != 0:
            err.seek(0)
            skipped.append("tar error: " + err.read().decode("utf-8", "replace").strip())
    return count, skipped


# --------------------------------------------------------------------------- #
# Running prompts
# --------------------------------------------------------------------------- #
def summarize_tool_input(name: str, data: dict) -> str:
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description"):
        if key in data and data[key]:
            return str(data[key])
    if name == "TodoWrite" and isinstance(data.get("todos"), list):
        return f"{len(data['todos'])} todos"
    return json.dumps(data, ensure_ascii=False)


def tool_result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content or "")


def render_event(ev: dict) -> list[tuple[str, str]]:
    """Return (live one-liner, transcript markdown) pairs for a stream-json event."""
    out: list[tuple[str, str]] = []
    etype = ev.get("type")
    if etype == "system" and ev.get("subtype") == "init":
        msg = f"session started - model {ev.get('model', '?')}"
        out.append((msg, f"_{msg}_"))
    elif etype == "assistant":
        for block in (ev.get("message") or {}).get("content") or []:
            btype = block.get("type")
            if btype == "text" and block.get("text", "").strip():
                text = block["text"].strip()
                out.append(("says: " + one_line(text), text))
            elif btype == "tool_use":
                summary = f"{block.get('name')}: {summarize_tool_input(block.get('name', ''), block.get('input') or {})}"
                out.append(("> " + one_line(summary), f"- tool `{one_line(summary, 400)}`"))
    elif etype == "user":
        content = (ev.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    text = tool_result_text(block)
                    out.append(("! tool error: " + one_line(text), f"  - tool error: {one_line(text, 400)}"))
    elif etype == "result":
        msg = (f"finished ({ev.get('subtype')}) - {ev.get('num_turns')} agent turns, "
               f"{fmt_secs(ev.get('duration_ms'))}, {fmt_cost(ev.get('total_cost_usd'))}")
        out.append((msg, f"_{msg}_"))
    return out


def count_tool_calls(ev: dict) -> int:
    if ev.get("type") != "assistant":
        return 0
    return sum(1 for b in (ev.get("message") or {}).get("content") or []
               if isinstance(b, dict) and b.get("type") == "tool_use")


def run_turn(agent: Agent, prompt: str, run: "Run") -> dict:
    args, console = run.args, run.console
    n = len(agent.turns) + 1
    cmd = ["docker", "exec", "-i", "-w", WORKSPACE, agent.container,
           "claude", "-p", "--dangerously-skip-permissions",
           "--output-format", "stream-json", "--verbose"]
    if args.model:
        cmd += ["--model", args.model]
    if args.max_turns:
        cmd += ["--max-turns", str(args.max_turns)]
    if agent.session_id:
        cmd += ["--resume", agent.session_id]

    record: dict = {"turn": n, "prompt": prompt, "status": "running",
                    "started_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "resumed_session": agent.session_id}
    agent.stop_requested = False
    timed_out = threading.Event()
    timer = None
    result_ev: dict | None = None
    tool_calls = 0
    t0 = time.monotonic()

    agent.log_dir.mkdir(parents=True, exist_ok=True)
    with open(agent.log_dir / f"turn_{n:02d}.jsonl", "w", encoding="utf-8") as raw_log, \
            open(agent.log_dir / "transcript.md", "a", encoding="utf-8") as transcript:
        transcript.write(f"\n\n## Turn {n}\n\n### Prompt\n\n{prompt}\n\n### Activity\n\n")
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        if args.timeout:
            def on_timeout() -> None:
                timed_out.set()
                console.agent(agent, f"timeout of {args.timeout} min reached - stopping")
                stop_agent_process(agent)
            timer = threading.Timer(args.timeout * 60, on_timeout)
            timer.daemon = True
            timer.start()
        try:
            assert proc.stdin is not None
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            raw_log.write(line + "\n")
            raw_log.flush()
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                ev = None
            if not isinstance(ev, dict):
                if line.strip():
                    console.agent(agent, line)
                    transcript.write(f"    {line}\n")
                continue
            if ev.get("session_id"):
                agent.session_id = ev["session_id"]
            if ev.get("type") == "result":
                result_ev = ev
            tool_calls += count_tool_calls(ev)
            for live, full in render_event(ev):
                if not args.quiet:
                    console.agent(agent, live)
                transcript.write(full + "\n\n")
        exit_code = proc.wait()
        if timer:
            timer.cancel()

        final_text = (result_ev or {}).get("result") or ""
        transcript.write(f"\n### Final response\n\n{final_text or '_(none)_'}\n")

    usage = (result_ev or {}).get("usage") or {}
    if timed_out.is_set():
        status = "timeout"
    elif agent.stop_requested:
        status = "interrupted"
    elif result_ev is None:
        status = f"failed (exit {exit_code})"
    elif result_ev.get("is_error"):
        status = "error: " + one_line(result_ev.get("result") or result_ev.get("subtype") or "?", 50)
    else:
        status = "ok"
    record.update({
        "status": status,
        "exit_code": exit_code,
        "wall_time_s": round(time.monotonic() - t0, 1),
        "duration_ms": (result_ev or {}).get("duration_ms"),
        "agent_turns": (result_ev or {}).get("num_turns"),
        "tool_calls": tool_calls,
        "cost_usd": (result_ev or {}).get("total_cost_usd"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
        "session_id": agent.session_id,
        "result": final_text,
    })
    agent.turns.append(record)
    return record


# --------------------------------------------------------------------------- #
# Run orchestration
# --------------------------------------------------------------------------- #
@dataclass
class Run:
    args: argparse.Namespace
    console: Console
    run_id: str
    run_dir: Path
    mode: str
    project: Path | None
    agents_md: Path
    agents: list[Agent]
    claude_version: str = ""
    removed_instruction_files: list[str] = field(default_factory=list)
    exported_after_round: int = -1
    rounds: int = 0
    bridge_used: bool = False

    def agent(self, key: str) -> Agent:
        return next(a for a in self.agents if a.key == key)

    def save(self) -> None:
        data = {
            "run_id": self.run_id,
            "mode": self.mode,
            "project": str(self.project) if self.project else None,
            "agents_md": str(self.agents_md),
            "claude_code_version": self.claude_version,
            "model": self.args.model,
            "claude_md_bridge": self.bridge_used,
            "removed_instruction_files": self.removed_instruction_files,
            "agents": {a.key: {"title": a.title, "container": a.container,
                               "session_id": a.session_id, "turns": a.turns}
                       for a in self.agents},
        }
        (self.run_dir / "run.json").write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                               encoding="utf-8")
        (self.run_dir / "summary.md").write_text(self.summary_markdown(), encoding="utf-8")

    def summary_rows(self) -> list[list[str]]:
        rows = []
        for a in self.agents:
            for t in a.turns:
                rows.append([str(t["turn"]), a.tag, t["status"], f"{t['wall_time_s']:.0f}s",
                             str(t.get("agent_turns") or "-"), str(t["tool_calls"]),
                             str(t.get("output_tokens") or "-"), fmt_cost(t.get("cost_usd")),
                             one_line(t["prompt"], 40)])
        return rows

    SUMMARY_HEADERS = ["Turn", "Agent", "Status", "Wall time", "Agent turns",
                       "Tool calls", "Output tok", "Cost", "Prompt"]

    def summary_markdown(self) -> str:
        lines = [f"# AGENTS.md A/B run {self.run_id}", "",
                 f"- Mode: {self.mode}" + (f" (project: `{self.project}`)" if self.project else ""),
                 f"- AGENTS.md under test: `{self.agents_md}`",
                 f"- Claude Code: {self.claude_version}",
                 f"- Model: {self.args.model or 'default'}", "",
                 "| " + " | ".join(self.SUMMARY_HEADERS) + " |",
                 "|" + "---|" * len(self.SUMMARY_HEADERS)]
        lines += ["| " + " | ".join(c.replace("|", "\\|") for c in r) + " |"
                  for r in self.summary_rows()]
        for a in self.agents:
            total = sum(t.get("cost_usd") or 0 for t in a.turns)
            wall = sum(t["wall_time_s"] for t in a.turns)
            lines.append(f"\n**{a.title}** - total wall time {wall:.0f}s, total cost {fmt_cost(total)}")
        return "\n".join(lines) + "\n"

    def print_summary(self) -> None:
        rows = self.summary_rows()
        if not rows:
            self.console.print("No prompts have been run yet.")
            return
        table = [self.SUMMARY_HEADERS] + rows
        widths = [max(len(r[i]) for r in table) for i in range(len(self.SUMMARY_HEADERS))]
        self.console.header("Results summary")
        for i, r in enumerate(table):
            self.console.print("  ".join(c.ljust(w) for c, w in zip(r, widths)),
                               "bold" if i == 0 else None)
        for a in self.agents:
            total = sum(t.get("cost_usd") or 0 for t in a.turns)
            wall = sum(t["wall_time_s"] for t in a.turns)
            self.console.print(f"{a.title}: total wall time {wall:.0f}s, total cost {fmt_cost(total)}")

    def print_last_responses(self, agents: list[Agent] | None = None) -> None:
        for a in agents or self.agents:
            if not a.turns:
                continue
            t = a.turns[-1]
            self.console.header(f"{a.title} - final response (turn {t['turn']}, {t['status']})", a.key)
            self.console.print(t["result"] or "(no final response)")


def run_round(run: Run, agents: list[Agent], prompt: str) -> None:
    console = run.console
    run.rounds += 1
    with (run.run_dir / "prompts.md").open("a", encoding="utf-8") as fh:
        fh.write(f"\n\n## Round {run.rounds} -> {', '.join(a.title for a in agents)}\n\n{prompt}\n")
    console.header(f"Round {run.rounds}: sending prompt to {' and '.join(a.title for a in agents)}")
    console.print("Press Ctrl+C to stop the agents.", "dim")
    sync_credentials(run.agents)

    errors: dict[str, BaseException] = {}

    def worker(agent: Agent) -> None:
        try:
            run_turn(agent, prompt, run)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors[agent.key] = exc

    threads = [threading.Thread(target=worker, args=(a,), daemon=True) for a in agents]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=0.3)
    except KeyboardInterrupt:
        console.warn("Interrupted - stopping agents (their session is kept, you can continue).")
        for a in agents:
            stop_agent_process(a)
        for t in threads:
            t.join(timeout=60)
    for key, exc in errors.items():
        console.error(f"Agent {key.upper()} crashed: {exc}")
    sync_credentials(run.agents)

    run.print_last_responses(agents)
    run.print_summary()
    run.save()


def export_dialog(run: Run) -> None:
    console = run.console
    last_turn = max((len(a.turns) for a in run.agents), default=0)
    default = run.run_dir / "workspaces" / f"after_round_{run.rounds:02d}"
    suffix = 2
    while default.exists():
        default = run.run_dir / "workspaces" / f"after_round_{run.rounds:02d}_{suffix}"
        suffix += 1
    dest = Path(ask("Copy the workspaces to which folder?", str(default))).expanduser()
    skip_heavy = ask_yes_no(f"Skip dependency/cache folders ({', '.join(HEAVY_DIRS[:3])}, ...)?", True)
    for a in run.agents:
        target = dest / a.folder
        if target.exists() and any(target.iterdir()):
            if not ask_yes_no(f"{target} is not empty. Copy into it anyway?", False):
                continue
        console.print(f"Copying {a.title} workspace -> {target} ...")
        count, skipped = export_workspace(a, target, skip_heavy)
        console.info(f"  {count} files copied")
        for s in skipped[:10]:
            console.warn(f"  skipped {s}")
        if len(skipped) > 10:
            console.warn(f"  ... and {len(skipped) - 10} more skipped entries")
    run.exported_after_round = run.rounds
    console.info(f"Workspaces copied to {dest} (after {last_turn} turn(s)).")


MENU = """
What next?
  1) Send a prompt to BOTH agents
  2) Send a prompt to agent A only (with AGENTS.md)
  3) Send a prompt to agent B only (baseline)
  4) Copy both workspaces to the host
  5) Show results summary
  6) Show the last responses again
  7) Show commands to inspect the containers yourself
  q) Quit (removes the containers)"""


def interactive_loop(run: Run) -> None:
    console = run.console
    a, b = run.agent("a"), run.agent("b")
    while True:
        console.print(MENU, "bold")
        try:
            choice = input("> ").strip().lower()
        except EOFError:
            choice = "q"
        try:
            if choice in ("1", "2", "3"):
                targets = {"1": [a, b], "2": [a], "3": [b]}[choice]
                prompt = read_prompt(console, f"Prompt for {' and '.join(t.title for t in targets)}:")
                if prompt:
                    run_round(run, targets, prompt)
                else:
                    console.warn("Empty prompt - nothing sent.")
            elif choice == "4":
                export_dialog(run)
            elif choice == "5":
                run.print_summary()
            elif choice == "6":
                run.print_last_responses()
            elif choice == "7":
                for ag in run.agents:
                    console.print(f"{ag.title}:\n  docker exec -it {ag.container} bash\n"
                                  f"  docker exec -it -w {WORKSPACE} {ag.container} claude --resume {ag.session_id or ''}")
                console.print(f"Logs and transcripts: {run.run_dir}", "dim")
            elif choice in ("q", "quit", "exit"):
                if run.exported_after_round != run.rounds and ask_yes_no(
                        "The latest workspaces have not been copied to the host. Copy them now?", True):
                    export_dialog(run)
                return
        except EOFError:
            return
        except KeyboardInterrupt:
            console.print("\n(cancelled)")


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="A/B test an AGENTS.md file with two isolated Claude Code agents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              python agentsmd_tester.py                          # fully interactive
              python agentsmd_tester.py --agents-md AGENTS.md --prompt-file task.md
              python agentsmd_tester.py --agents-md AGENTS.md --project ../my-app --model opus
            """))
    p.add_argument("--agents-md", type=Path, help="AGENTS.md under test (given to agent A only)")
    p.add_argument("--mode", choices=["new", "existing"],
                   help="'new': start from an empty workspace; 'existing': copy --project in")
    p.add_argument("--project", type=Path, help="existing project copied into both workspaces")
    p.add_argument("--prompt", help="initial prompt text")
    p.add_argument("--prompt-file", type=Path, help="read the initial prompt from a file")
    p.add_argument("--model", help="model passed to both agents (e.g. opus, sonnet, claude-opus-5-5)")
    p.add_argument("--max-turns", type=int, help="limit agentic turns per prompt")
    p.add_argument("--timeout", type=float, help="per-prompt timeout in minutes (default: none)")
    p.add_argument("--claude-version", default="latest",
                   help="Claude Code npm version baked into the image (default: latest)")
    p.add_argument("--rebuild", action="store_true", help="rebuild the image without cache")
    p.add_argument("--exclude", action="append", default=[], metavar="NAME",
                   help="extra file/dir name to skip when copying the project in (repeatable)")
    p.add_argument("--keep-instruction-files", action="store_true",
                   help="keep CLAUDE.md / nested AGENTS.md files of the project in BOTH copies "
                        "(the root AGENTS.md is always replaced for A and removed for B)")
    p.add_argument("--claude-md-bridge", choices=["auto", "always", "never"], default="auto",
                   help="add '@AGENTS.md' to a root CLAUDE.md in agent A's workspace. Claude Code "
                        "loads AGENTS.md natively only when the project has no CLAUDE.md, so "
                        "'auto' (default) adds it only when a project CLAUDE.md is kept")
    p.add_argument("--memory", help="memory limit per container, e.g. 4g")
    p.add_argument("--cpus", help="CPU limit per container, e.g. 2")
    p.add_argument("--keep-containers", action="store_true", help="do not remove containers on exit")
    p.add_argument("--runs-dir", type=Path, default=ROOT / "runs", help="where run logs are written")
    p.add_argument("--quiet", action="store_true", help="do not stream agent activity live")
    p.add_argument("--no-color", action="store_true")
    return p.parse_args()


def resolve_inputs(args: argparse.Namespace, console: Console) -> tuple[str, Path | None, Path, str]:
    mode = args.mode or ("existing" if args.project else None)
    if mode is None:
        console.print("\nUse case:\n  1) Start a project from scratch\n  2) Edit an existing project", "bold")
        while mode is None:
            mode = {"1": "new", "2": "existing"}.get(ask("Choose 1 or 2", "1"))

    project: Path | None = None
    if mode == "existing":
        project = args.project
        while project is None or not project.is_dir():
            if project is not None:
                console.error(f"Not a directory: {project}")
            project = Path(ask("Path of the existing project to copy")).expanduser()
        project = project.resolve()
        if SECRETS_DIR.resolve().is_relative_to(project):
            raise SystemExit(f"Refusing to copy {project}: it contains the credentials folder {SECRETS_DIR}.")

    agents_md = args.agents_md
    if agents_md is None:
        default = next((str(p) for p in (Path.cwd() / "AGENTS.md", ROOT / "AGENTS.md") if p.is_file()), None)
        agents_md = Path(ask("Path of the AGENTS.md under test", default))
    while not agents_md.is_file():
        console.error(f"File not found: {agents_md}")
        agents_md = Path(ask("Path of the AGENTS.md under test"))
    agents_md = agents_md.resolve()

    if args.prompt_file:
        prompt = args.prompt_file.read_text(encoding="utf-8").strip()
    elif args.prompt:
        prompt = args.prompt.strip()
    else:
        prompt = ""
    while not prompt:
        prompt = read_prompt(console, "Initial prompt (sent to both agents):")
    return mode, project, agents_md, prompt


def check_credentials(console: Console) -> None:
    if CREDENTIALS_FILE.is_file() or ENV_FILE.is_file():
        return
    raise SystemExit(
        f"No credentials found. Provide one of:\n"
        f"  {CREDENTIALS_FILE}  (copy of ~/.claude/.credentials.json after `claude login`)\n"
        f"  {ENV_FILE}  (e.g. ANTHROPIC_API_KEY=... or CLAUDE_CODE_OAUTH_TOKEN=...)")


def prepare_workspaces(run: Run) -> None:
    console, args = run.console, run.args
    excludes = DEFAULT_COPY_IN_EXCLUDES | set(args.exclude)
    strip = not args.keep_instruction_files

    a_files = {"AGENTS.md": run.agents_md.read_bytes()}
    existing = run.project / "CLAUDE.md" if run.project else None
    kept_claude_md = (not strip and run.project is not None
                      and any((run.project / p).is_file() for p in ("CLAUDE.md", ".claude/CLAUDE.md")))
    if args.claude_md_bridge == "always" or (args.claude_md_bridge == "auto" and kept_claude_md):
        bridge = b"@AGENTS.md\n"
        if existing and existing.is_file() and not strip:
            bridge = existing.read_bytes().rstrip() + b"\n\n" + bridge
        a_files["CLAUDE.md"] = bridge
        run.bridge_used = True
        console.print("Agent A: root CLAUDE.md imports @AGENTS.md (bridge enabled).", "dim")

    with tempfile.TemporaryDirectory(prefix="agentsmd-") as tmp:
        for agent, extra in ((run.agent("a"), a_files), (run.agent("b"), {})):
            tar_path = Path(tmp) / f"{agent.key}.tar"
            removed = build_workspace_tar(tar_path, run.project, extra, excludes, strip)
            if agent.key == "b":
                run.removed_instruction_files = removed
            size_mb = tar_path.stat().st_size / 1e6
            console.print(f"Uploading workspace for {agent.title} ({size_mb:.1f} MB) ...")
            upload_workspace(agent, tar_path)

    if run.removed_instruction_files:
        console.warn("Instruction files found in the project were left out of both workspaces: "
                     + ", ".join(run.removed_instruction_files))
        if run.project and (run.project / ".git").exists():
            console.warn("The project is a git repository: if those files are committed, the agents "
                         "may still see them through git (e.g. `git status`, `git show`).")


def main() -> int:
    args = parse_args()
    console = Console(color=not args.no_color and sys.stdout.isatty())
    try:
        check_docker()
    except DockerError as exc:
        console.error(str(exc))
        return 1
    check_credentials(console)

    try:
        mode, project, agents_md, prompt = resolve_inputs(args, console)
    except (EOFError, KeyboardInterrupt):
        console.print("\nAborted.")
        return 1

    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = (args.runs_dir / run_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(agents_md, run_dir / "AGENTS.under-test.md")

    agents = [
        Agent("a", "A with AGENTS.md", "Agent A (with AGENTS.md)", "agent_a_with_agents_md",
              f"agentsmd-{run_id}-a", f"agentsmd-{run_id}-a-net", run_dir / "agent_a"),
        Agent("b", "B baseline      ", "Agent B (baseline, no AGENTS.md)", "agent_b_without_agents_md",
              f"agentsmd-{run_id}-b", f"agentsmd-{run_id}-b-net", run_dir / "agent_b"),
    ]
    run = Run(args, console, run_id, run_dir, mode, project, agents_md, agents)
    console.print(f"Run {run_id} - logs in {run_dir}", "dim")

    try:
        run.claude_version = build_image(console, args.claude_version, args.rebuild)
        if uses_credentials_file() and not refresh_credentials(console):
            return 1
        console.header("Starting containers")
        for a in agents:
            start_container(a, run_id, args)
            install_credentials(a)
            console.info(f"{a.title}: container {a.container}")
        prepare_workspaces(run)
        run.save()

        run_round(run, agents, prompt)
        interactive_loop(run)
    except DockerError as exc:
        console.error(str(exc))
        return 1
    except KeyboardInterrupt:
        console.print("\nAborted.")
    finally:
        run.save()
        try:
            sync_credentials(agents)  # keep the latest rotated token before containers go away
        except Exception as exc:  # noqa: BLE001
            console.warn(f"Could not save refreshed credentials: {exc}")
        if args.keep_containers:
            console.print("Containers kept: " + ", ".join(a.container for a in agents)
                          + f"\nRemove them with: docker rm -f {' '.join(a.container for a in agents)}"
                          + f" && docker network rm {' '.join(a.network for a in agents)}")
        else:
            console.print("Removing containers ...", "dim")
            remove_containers(agents)
        console.print(f"Run artifacts: {run_dir}", "dim")
    return 0


if __name__ == "__main__":
    sys.exit(main())
