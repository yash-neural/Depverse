# Depverse

[![Live Demo](https://img.shields.io/badge/Live%20Demo-View%20Presentation-C08763?style=for-the-badge&logo=github)](https://yash-neural.github.io/Depverse/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-1.8.0%2B-1a1a1a?style=flat-square)](https://modelcontextprotocol.io)
[![npm Registry](https://img.shields.io/badge/npm-registry-CB3837?style=flat-square&logo=npm&logoColor=white)](https://registry.npmjs.org)

> **🎬 [View the live presentation deck →](https://yash-neural.github.io/Depverse/)**
> A three-part walkthrough of MCP, building an MCP server, and the Depverse npm tools.

**Depverse** is an [MCP](https://modelcontextprotocol.io) (Model Context Protocol) server that exposes the public [npm Registry](https://registry.npmjs.org) as a set of structured tools Claude can call. It ships with a CLI chat client so you can talk to Claude in your terminal and let it inspect any npm package — versions, dependencies, changelogs, peer compatibility, bundle size, and more — without ever leaving the shell.

The server speaks the MCP `stdio` transport, so it plugs straight into Claude Desktop, Claude Code, or any other MCP-aware client.

---

## Features

Depverse exposes **39 tools** grouped into seven categories.

### Version tools
| Tool | What it does |
| --- | --- |
| `get_latest_version` | Latest stable version string of a package. |
| `get_all_versions` | Every published version (plus a count). |
| `get_version_info` | Manifest for a specific version (deps, license, engines, …). |
| `get_dist_tags` | All dist-tags (`latest`, `beta`, `next`, …) and the versions they point to. |
| `get_changelog` | Release notes from GitHub Releases (if a version is given) or `CHANGELOG.md` from the linked repo. |
| `check_version_exists` | Boolean check: is `pkg@version` published? |

### Package info tools
| Tool | What it does |
| --- | --- |
| `get_package_info` | High-level metadata card: name, description, author, license, homepage, maintainers, created/modified dates. |
| `get_package_readme` | README markdown for the latest version (truncated to 20,000 chars). |
| `get_package_keywords` | Keywords / tags declared in `package.json`. |
| `get_package_repository` | Source repo URL, plus a parsed `owner/repo` slug when the repo is on GitHub. |
| `get_package_homepage` | Homepage URL + npm page URL as a fallback. |
| `get_package_license` | Declared license (string, SPDX, or legacy array form). |
| `get_package_size` | Unpacked size (bytes + human-readable) and file count for a specific version. |

### Dependency tools
| Tool | What it does |
| --- | --- |
| `get_dependencies` | Runtime `dependencies` for a version. |
| `get_peer_dependencies` | `peerDependencies` + `peerDependenciesMeta` (marks optional peers). |
| `get_dev_dependencies` | `devDependencies` (build/test-time only). |
| `get_dependency_tree` | Walks the transitive dep graph. Resolves nodes in parallel, de-duplicates, and caps at `max_depth` (default 2, hard-capped at 4). |
| `check_peer_compatibility` | Given `{peer_name: installed_version}`, reports per-peer `yes` / `no` / `unknown` / `missing` / `missing-optional`. Ships a small semver matcher that handles `^`, `~`, `>=`, `<=`, `>`, `<`, `=`, `*`, `||`. |

### Security & Health tools
| Tool | What it does |
| --- | --- |
| `check_vulnerabilities` | Check a package + version against the [OSV.dev](https://osv.dev) database. Returns all matching advisories (GHSA, CVE) with severity and references. |
| `get_deprecation_status` | Reports whether a package or specific version is deprecated, plus the deprecation message. Scans all versions when no `version` is given. |
| `check_maintainer_activity` | Last publish date, publish count, average cadence, and a status label (`active` / `slowing` / `stale` / `abandoned`). |
| `get_download_stats` | Weekly / monthly download counts from the public npm download API, plus a simple popularity tier. |
| `check_typosquat_risk` | Flags names suspiciously close to popular packages via Levenshtein distance — catches common supply-chain typos. |
| `get_download_trend` | Day-by-day download counts over a range (`last-month`, `last-year`, or custom dates) with a `growing` / `declining` / `flat` trend label. |
| `compare_popularity` | Side-by-side download counts for 2–10 packages. Returns a ranking plus each package's share of the combined total. |
| `get_download_by_version` | Per-version download breakdown for the last week — shows which versions users are actually installing, plus the most popular major line. |

### Compatibility & Update tools
| Tool | What it does |
| --- | --- |
| `check_node_compatibility` | Returns the `engines` field (node / npm / yarn constraints) declared by a package version. |
| `compare_versions` | Diffs two versions' `dependencies`, `devDependencies`, `peerDependencies`, and `engines` — reports added / removed / range-changed. |
| `get_breaking_changes` | Scans a `from → to` version diff for direct or peer dependencies whose declared range crossed a **major** version boundary. |
| `resolve_semver` | Resolves an npm range (`^18.0.0`, `~4.17.20`, `>=2 <3`, `1.x`, `*`) to the highest published version that satisfies it. |
| `check_outdated` | Given `{package_name: installed_version}`, returns per-package `outdated` flag and gap level (`major` / `minor` / `patch`). Parallel fan-out. |

### Search & Discovery tools
| Tool | What it does |
| --- | --- |
| `search_packages` | Free-text search over the npm Registry with relevance / quality / popularity / maintenance scores. |
| `get_similar_packages` | Finds alternatives to a package by searching on its declared keywords — filters out the source package itself. |
| `get_packages_by_author` | All packages published by a given npm username (via `author:` qualifier). |
| `get_organization_packages` | All packages under a scope like `@babel` or `@vue`. Over-fetches + strict prefix filter for reliability. |

### Utility tools
| Tool | What it does |
| --- | --- |
| `batch_get_versions` | Parallel `/latest` lookup for a list of packages — one round-trip per package instead of sequential. |
| `validate_package_json` | Checks dep ranges in a `package.json` resolve to at least one published version. Flags typos like `lodash@999.0.0`. |
| `generate_install_command` | Builds install commands for npm / pnpm / yarn / bun with `--dev` and `--exact` flag dialects handled per-manager. |
| `resolve_cdn_url` | Pinned jsDelivr, unpkg, and esm.sh URLs for a package + optional file path. Auto-resolves "latest" when no version is given. |

All tools return JSON. Errors become `ValueError`s with a clear message (e.g. `"npm package 'foo' was not found."`), which MCP surfaces to the client as a tool error.

---

## Project layout

```
Depverse/
├── mcp_server.py       # The MCP server — all 18 npm tools live here
├── mcp_client.py       # Thin MCP client wrapper (stdio transport)
├── main.py             # Entrypoint for the CLI chat
├── test_npm_tool.py    # Manual end-to-end test for the server
├── core/
│   ├── chat.py         # Tool-using chat loop
│   ├── cli_chat.py     # CLI-flavoured chat (supports @docs and /commands)
│   ├── cli.py          # prompt-toolkit UI (autocompletion, history, key bindings)
│   ├── claude.py       # Anthropic API wrapper
│   └── tools.py        # Bridges MCP tool calls into Anthropic tool_use blocks
├── pyproject.toml
├── uv.lock
├── .mcp.json           # Example MCP server config for external clients
└── Presentation/       # Slides / HTML explainers for the project
```

---

## Prerequisites

- Python **3.10+**
- An [Anthropic API key](https://console.anthropic.com/)
- Optional but recommended: [uv](https://github.com/astral-sh/uv) for fast dependency management
- Network access to `registry.npmjs.org` and (for changelogs) `api.github.com` / `raw.githubusercontent.com`

---

## Setup

### 1. Configure environment variables

Create a `.env` file in the project root:

```env
ANTHROPIC_API_KEY="sk-ant-..."
CLAUDE_MODEL="claude-sonnet-4-5"
# Optional: set to 1 to launch the MCP server via `uv run` instead of `python`
USE_UV=1
```

### 2. Install dependencies

**Option A — with `uv` (recommended)**

```bash
pip install uv                # if you don't have it yet
uv venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
uv pip install -e .
```

**Option B — with plain `pip`**

```bash
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install anthropic httpx python-dotenv prompt-toolkit "mcp[cli]>=1.8.0"
```

---

## Running the CLI chat

```bash
uv run main.py
# or, without uv:
python main.py
```

You'll get a prompt:

```
> what versions of react are available?
> compare the peer deps of @tanstack/react-query 5.0.0 vs 4.36.0
> is express@5.0.0 published yet?
```

Claude automatically picks the right Depverse tool, calls it, and summarises the result.

### CLI features (from `prompt-toolkit`)
- **Tab-complete** for `/`-commands (slash commands exposed by the server as prompts).
- **`@`-mentions** for any doc resources the connected server exposes (Depverse itself doesn't expose docs, but you can connect additional servers — see below).
- **History** via ↑ / ↓ arrow keys.
- **Inline suggestions** when you start typing `/command `.

### Connecting extra MCP servers

`main.py` accepts any number of extra server scripts as positional arguments; each one is spawned over stdio and its tools become available alongside Depverse's:

```bash
uv run main.py path/to/other_server.py path/to/yet_another.py
```

---

## Using Depverse from other MCP clients

Depverse is a standard stdio MCP server — you can wire it into Claude Desktop, Claude Code, or any other MCP client by pointing their config at `mcp_server.py`.

Example (`.mcp.json` / Claude Desktop `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "Depverse": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/Depverse",
        "run",
        "mcp_server.py"
      ],
      "env": {}
    }
  }
}
```

Use `"command": "python"` and drop `"run"` from `args` if you aren't using `uv`.

---

## Manual end-to-end test

`test_npm_tool.py` spawns the server, lists its tools, and calls each one with a realistic input. Handy for verifying changes without the full Claude loop:

```bash
uv run test_npm_tool.py
```

It prints each tool call and the (truncated) JSON response, so you can eyeball the output.

---

## How it works

1. **`mcp_server.py`** registers every tool with `FastMCP` (from the MCP Python SDK). Each tool is an `async` function that talks to the npm Registry over `httpx`, with a shared `_fetch_json` helper that enforces a 10 s timeout and consistent 404 / error messaging.
2. **`mcp_client.py`** wraps `mcp.ClientSession` with a small context-managed class. It exposes `list_tools`, `call_tool`, `list_prompts`, `get_prompt`, and `read_resource`.
3. **`core/chat.py`** drives the tool-use loop: send the message + tool definitions to Claude, execute any `tool_use` blocks via `ToolManager`, append the results, repeat until Claude stops calling tools.
4. **`core/cli.py`** wraps all of the above in a `prompt-toolkit` UI.

The design cleanly separates the MCP side (server + client transport) from the chat side (Claude wrapper + CLI), so each piece can be reused on its own — you can use the server without the chat, or point the chat at completely different MCP servers.

---

## License

No license file is committed yet — add one (MIT is a sensible default) before publishing or accepting external contributions.
