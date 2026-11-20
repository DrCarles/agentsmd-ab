# Sandbox image shared by both agents. Both containers are created from this
# exact image, so the only difference between agent A and agent B is the
# AGENTS.md file placed in A's workspace.
#
# Add any toolchains your test projects need (Go, Rust, Java, ...) here: both
# agents get them equally.
FROM node:22-bookworm

ARG CLAUDE_CODE_VERSION=latest
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential ca-certificates curl git jq less procps ripgrep unzip \
      python3 python3-pip python3-venv \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
 && npm cache clean --force

RUN mkdir -p /workspace && chown node:node /workspace

# --dangerously-skip-permissions refuses to run as root, so agents run as `node`.
USER node
ENV DISABLE_AUTOUPDATER=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1
RUN mkdir -p /home/node/.claude \
 && git config --global user.name "Agent" \
 && git config --global user.email "agent@example.invalid" \
 && git config --global init.defaultBranch main

WORKDIR /workspace
CMD ["sleep", "infinity"]
