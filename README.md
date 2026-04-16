# Depverse

[![Docs](https://img.shields.io/badge/Docs-View%20Full%20Docs-C08763?style=for-the-badge&logo=github)](https://yash-neural.github.io/Depverse/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-1.8.0%2B-1a1a1a?style=flat-square)](https://modelcontextprotocol.io)
[![npm Registry](https://img.shields.io/badge/npm-registry-CB3837?style=flat-square&logo=npm&logoColor=white)](https://registry.npmjs.org)
[![Tools](https://img.shields.io/badge/Tools-39-C08763?style=flat-square)](https://yash-neural.github.io/Depverse/#tool-reference)

> **📖 [Full documentation →](https://yash-neural.github.io/Depverse/)**
> Install, tool reference, and setup guides for Claude Code, Claude Desktop, Cursor, Cline, Windsurf, and Copilot.

**Depverse** is an [MCP](https://modelcontextprotocol.io) (Model Context Protocol) server that exposes the public [npm Registry](https://registry.npmjs.org) as 39 structured tools that Claude (or any MCP-aware client) can call — versions, dependencies, changelogs, security advisories, download trends, and more — without ever leaving the editor.

The server speaks MCP's `stdio` transport, so it plugs straight into Claude Code, Claude Desktop, Cursor, Cline, Windsurf, and Copilot Chat. **No API key required** — the client brings its own auth.

---

## Features

Depverse exposes **44 tools** grouped into eight categories.

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

### Bundle Size tools *(via bundlephobia.com)*
| Tool | What it does |
| --- | --- |
| `get_bundle_size` | Minified + gzipped size of a package (with a specific or latest version), plus dependency count and ESM availability. |
| `get_bundle_size_history` | Size history across recent versions. Reports `growing` / `stable` / `shrinking` trend and percent delta. |
| `check_treeshakeable` | Returns `true` when the package ships ES modules AND declares `"sideEffects": false` — the two conditions needed for bundler tree-shaking. |
| `compare_bundle_sizes` | Parallel size lookup for 2–10 packages. Ranks by gzipped size (lightest first). |
| `get_bundle_size_impact` | Framed for PR-review: "adding X will add Y KB gzipped with Z transitive deps" — plus an `impact` tier (tiny / small / moderate / heavy). |

All tools return JSON. Errors become `ValueError`s with a clear message (e.g. `"npm package 'foo' was not found."`), which MCP surfaces to the client as a tool error.

---

## Project layout

```
Depverse/
├── mcp_server.py       # The MCP server — all 39 npm tools live here
├── mcp_client.py       # Thin MCP client wrapper (stdio transport)
├── test_npm_tool.py    # Manual end-to-end test for the server
├── pyproject.toml
├── uv.lock
├── .mcp.json           # Example MCP server config for external clients
├── docs/               # Documentation site (GitHub Pages)
│
│   # --- Optional: bundled CLI chat (main.py) ---
├── main.py             # Entrypoint for the optional CLI chat
└── core/
    ├── chat.py         # Tool-using chat loop
    ├── cli_chat.py     # CLI-flavoured chat (supports @docs and /commands)
    ├── cli.py          # prompt-toolkit UI (autocompletion, history, key bindings)
    ├── claude.py       # Anthropic API wrapper
    └── tools.py        # Bridges MCP tool calls into Anthropic tool_use blocks
```

---

## Prerequisites

- Python **3.10+**
- [uv](https://github.com/astral-sh/uv) (recommended) or plain `pip`
- Network access to `registry.npmjs.org`, `api.osv.dev`, `api.npmjs.org` and (for changelogs) `api.github.com` / `raw.githubusercontent.com`

> **No Anthropic API key required.** Claude Code (or any MCP client) brings its own auth. A key is only needed if you also want to use the optional bundled CLI chat (`main.py`).

---

## Install

```bash
git clone https://github.com/yash-neural/Depverse.git
cd Depverse
uv venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
uv pip install -e .
```

Or without `uv`:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Verify the server starts cleanly:

```bash
uv run test_npm_tool.py   # spawns the server, lists tools, calls each once
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

1. **`mcp_server.py`** registers every tool with `FastMCP` (from the MCP Python SDK). Each tool is an `async` function that talks to the npm Registry, OSV.dev, or the npm download API over `httpx`, with a shared `_fetch_json` helper that enforces a 10 s timeout and consistent 404 / error messaging.
2. **`mcp_client.py`** wraps `mcp.ClientSession` with a small context-managed class — only used by `test_npm_tool.py` and the optional CLI chat.
3. The MCP client (Claude Code, Claude Desktop, Cursor, etc.) spawns `mcp_server.py` as a subprocess over stdio. JSON-RPC frames flow in both directions; tool calls return structured JSON the model can reason about.

The MCP server is the whole point — everything in `core/` is scaffolding for the **optional** bundled CLI chat, which you can ignore if you're just plugging Depverse into Claude Code.

---

## License

No license file is committed yet — add one (MIT is a sensible default) before publishing or accepting external contributions.
