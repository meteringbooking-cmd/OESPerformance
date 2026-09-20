# MCP servers used on this project

This project uses two [MCP](https://modelcontextprotocol.io/) servers with Claude Code. Neither is required to run the app — they're developer tooling that makes working on this repo with an AI assistant more effective. This doc records what's configured, why, and how to reproduce the setup on another machine.

## github

**What it is:** the official GitHub MCP server (`@modelcontextprotocol/server-github`), giving the assistant direct access to this repo's issues, PRs, CI status, and branches on GitHub.

**Why:** the repo lives at `github.com/meteringbooking-cmd/OESPerformance`. Without this, getting PR/issue context requires manually pasting things in; with it, the assistant can look things up directly.

**Setup:**
```
claude mcp add github -e GITHUB_PERSONAL_ACCESS_TOKEN="$(gh auth token)" -- npx -y @modelcontextprotocol/server-github
```
This reuses whatever token the GitHub CLI (`gh`) already has from `gh auth login` — no separate token needs to be created or stored, as long as `gh auth status` shows you're logged in first.

## firebase

**What it is:** the Firebase CLI's built-in MCP server (`firebase-tools experimental:mcp`, aliased as `firebase mcp` in recent versions).

**Why:** this app's entire data layer is Firestore (project `oesperformance-a54c0`), plus Firebase Auth and Storage for a handful of pages. This lets the assistant inspect collections/documents directly instead of guessing from the HTML/JS that reads them — especially useful now that the [local emulator setup](LOCAL-DEVELOPMENT.md) exists, since the MCP server can talk to either the emulator or the real project depending on what you're logged into.

**Setup:**
```
claude mcp add firebase -- npx -y firebase-tools@latest experimental:mcp
```
For it to do anything useful against the **real** project (as opposed to the local emulator), you also need to be logged in:
```
firebase login
```
(This opens a browser OAuth flow — run it yourself rather than through the assistant.) Without running `firebase login`, the MCP server still starts, but calls against the real project will fail. This is fine if you're only working against the local emulator (see [LOCAL-DEVELOPMENT.md](LOCAL-DEVELOPMENT.md)) — no login is required for that.

## Checking what's configured

```
claude mcp list
```
Should show both `github` and `firebase` as `✔ Connected`.
