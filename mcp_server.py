"""
MCP Server: npm Registry tools

Exposes a set of tools Claude can call to inspect public npm packages.
Everything goes through the public npm Registry API:
    https://registry.npmjs.org
(docs: https://github.com/npm/registry/blob/main/docs/REGISTRY-API.md)

Tools provided:
    get_latest_version     - latest stable version string
    get_all_versions       - list of every published version
    get_version_info       - full manifest for a specific version
    get_dist_tags          - dist-tags map (latest, beta, next, ...)
    get_changelog          - release notes, sourced from GitHub if linked
    check_version_exists   - boolean check for a specific version
"""

import asyncio
import httpx
from pydantic import Field
from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# Server bootstrap
# ---------------------------------------------------------------------------
# FastMCP is the high-level helper from the MCP Python SDK. It registers
# tools/resources/prompts behind a clean decorator API and handles all the
# JSON-RPC plumbing for us. log_level="ERROR" keeps stdout clean so the
# stdio transport is not polluted with log lines.
mcp = FastMCP("Depverse", log_level="ERROR")


# Base URL for every npm Registry call.
NPM_REGISTRY = "https://registry.npmjs.org"

# Shared httpx timeout — we want a clear failure instead of a hanging tool.
HTTP_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Internal helper: fetch JSON from any URL with consistent error handling
# ---------------------------------------------------------------------------
# Centralising this means every tool below gets the same treatment:
#   - same timeout
#   - same error messages
#   - same handling for 404 vs. other HTTP errors
async def _fetch_json(url: str, not_found_msg: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(url)
    except httpx.RequestError as exc:
        raise ValueError(f"Could not reach {url}: {exc}") from exc

    if response.status_code == 404:
        raise ValueError(not_found_msg)

    if response.status_code >= 400:
        raise ValueError(
            f"Registry returned HTTP {response.status_code} for {url}"
        )

    return response.json()


# ---------------------------------------------------------------------------
# TOOL 1: get_latest_version
# ---------------------------------------------------------------------------
# Returns just the latest stable version string (e.g. "19.2.5").
# Under the hood, the /latest endpoint returns the full manifest for whatever
# the "latest" dist-tag currently points to.
@mcp.tool(
    name="get_latest_version",
    description="Get the latest stable version string of an npm package.",
)
async def get_latest_version(
    package_name: str = Field(
        description="Exact npm package name, e.g. 'react' or '@types/node'."
    ),
) -> dict:
    data = await _fetch_json(
        f"{NPM_REGISTRY}/{package_name}/latest",
        not_found_msg=f"npm package '{package_name}' was not found.",
    )
    return {
        "name": data.get("name", package_name),
        "version": data.get("version", "unknown"),
    }


# ---------------------------------------------------------------------------
# TOOL 2: get_all_versions
# ---------------------------------------------------------------------------
# The root /{pkg} endpoint returns a "package document" whose "versions" field
# maps every published version -> its manifest. We just need the keys, sorted
# in the order they were published (npm preserves publish order naturally).
@mcp.tool(
    name="get_all_versions",
    description=(
        "List every published version of an npm package. Returns the full "
        "list plus a count."
    ),
)
async def get_all_versions(
    package_name: str = Field(description="Exact npm package name."),
) -> dict:
    data = await _fetch_json(
        f"{NPM_REGISTRY}/{package_name}",
        not_found_msg=f"npm package '{package_name}' was not found.",
    )
    versions = list(data.get("versions", {}).keys())
    return {
        "name": data.get("name", package_name),
        "count": len(versions),
        "versions": versions,
    }


# ---------------------------------------------------------------------------
# TOOL 3: get_version_info
# ---------------------------------------------------------------------------
# Fetches the manifest for a single specific version. Useful for seeing what
# dependencies / exports / engines a given version declared.
@mcp.tool(
    name="get_version_info",
    description=(
        "Get metadata (dependencies, description, license, repository, etc.) "
        "for a specific version of an npm package."
    ),
)
async def get_version_info(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        description="The exact version string, e.g. '18.2.0' (no leading 'v')."
    ),
) -> dict:
    data = await _fetch_json(
        f"{NPM_REGISTRY}/{package_name}/{version}",
        not_found_msg=(
            f"Version '{version}' of '{package_name}' was not found."
        ),
    )
    # Trim the manifest down to the interesting fields — the full document
    # includes a lot of noise (shasum, tarball URL, _npmUser, etc.).
    return {
        "name": data.get("name"),
        "version": data.get("version"),
        "description": data.get("description", ""),
        "license": data.get("license", ""),
        "homepage": data.get("homepage", ""),
        "repository": data.get("repository", {}),
        "dependencies": data.get("dependencies", {}),
        "devDependencies": data.get("devDependencies", {}),
        "engines": data.get("engines", {}),
    }


# ---------------------------------------------------------------------------
# TOOL 4: get_dist_tags
# ---------------------------------------------------------------------------
# dist-tags are human-readable labels that point to a specific version, e.g.
#   latest -> 18.2.0
#   next   -> 19.0.0-rc.1
#   beta   -> 18.3.0-beta.2
# We read them from the package document's "dist-tags" key (simpler and more
# reliable than the /-/package/{pkg}/dist-tags endpoint).
@mcp.tool(
    name="get_dist_tags",
    description=(
        "Get all dist-tags for an npm package (latest, beta, next, etc.) "
        "mapped to their current version."
    ),
)
async def get_dist_tags(
    package_name: str = Field(description="Exact npm package name."),
) -> dict:
    data = await _fetch_json(
        f"{NPM_REGISTRY}/{package_name}",
        not_found_msg=f"npm package '{package_name}' was not found.",
    )
    return {
        "name": data.get("name", package_name),
        "dist_tags": data.get("dist-tags", {}),
    }


# ---------------------------------------------------------------------------
# TOOL 5: get_changelog
# ---------------------------------------------------------------------------
# npm doesn't serve changelogs directly, but most packages link a GitHub repo
# in their manifest. We use that to find the changelog in two ways:
#   1. If a version is given, try the GitHub Releases API for that tag.
#   2. Otherwise, try to fetch CHANGELOG.md from the repo's default branch.
# Both hops are best-effort — we return whatever we can and always explain
# the source in the response.
def _parse_github_slug(repository: dict | str | None) -> str | None:
    """Extract 'owner/repo' from an npm manifest's repository field."""
    if not repository:
        return None
    url = (
        repository.get("url", "")
        if isinstance(repository, dict)
        else str(repository)
    )
    if "github.com" not in url:
        return None
    # Strip git+ prefix, .git suffix, protocol — leave owner/repo.
    url = url.replace("git+", "").replace(".git", "")
    # github.com/<owner>/<repo>  OR  github:<owner>/<repo>
    if "github.com/" in url:
        slug = url.split("github.com/", 1)[1]
    elif url.startswith("github:"):
        slug = url[len("github:"):]
    else:
        return None
    return slug.strip("/")


@mcp.tool(
    name="get_changelog",
    description=(
        "Fetch changelog / release notes for an npm package. Pulls from "
        "GitHub Releases when a version is given, otherwise tries to fetch "
        "CHANGELOG.md from the linked GitHub repo."
    ),
)
async def get_changelog(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        default="",
        description=(
            "Optional exact version. If provided, we fetch the GitHub release "
            "notes for that tag. Leave empty to fetch the full CHANGELOG.md."
        ),
    ),
) -> dict:
    # Step 1: look up the package to find its repository.
    pkg = await _fetch_json(
        f"{NPM_REGISTRY}/{package_name}",
        not_found_msg=f"npm package '{package_name}' was not found.",
    )
    slug = _parse_github_slug(pkg.get("repository"))
    if not slug:
        raise ValueError(
            f"'{package_name}' does not declare a GitHub repository, "
            "so no changelog source is available."
        )

    # Step 2a: specific version -> GitHub Releases API.
    if version:
        # GitHub tags are usually "v1.2.3" but sometimes just "1.2.3".
        for tag in (f"v{version}", version):
            url = f"https://api.github.com/repos/{slug}/releases/tags/{tag}"
            try:
                release = await _fetch_json(url, not_found_msg="__skip__")
                return {
                    "source": "github-releases",
                    "repo": slug,
                    "tag": release.get("tag_name"),
                    "name": release.get("name"),
                    "published_at": release.get("published_at"),
                    "body": release.get("body", ""),
                }
            except ValueError as exc:
                if "__skip__" in str(exc):
                    continue
                raise
        raise ValueError(
            f"No GitHub release found for tag v{version} / {version} "
            f"in {slug}."
        )

    # Step 2b: no version -> try CHANGELOG.md from main then master.
    for branch in ("main", "master", "HEAD"):
        url = f"https://raw.githubusercontent.com/{slug}/{branch}/CHANGELOG.md"
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                r = await client.get(url)
            if r.status_code == 200:
                # Keep the response size reasonable — changelogs can be huge.
                text = r.text
                return {
                    "source": "github-changelog",
                    "repo": slug,
                    "branch": branch,
                    "truncated": len(text) > 20_000,
                    "content": text[:20_000],
                }
        except httpx.RequestError:
            continue

    raise ValueError(
        f"Could not find CHANGELOG.md in {slug} (tried main, master, HEAD)."
    )


# ---------------------------------------------------------------------------
# TOOL 6: check_version_exists
# ---------------------------------------------------------------------------
# Simple HEAD-style check: hit /{pkg}/{version} and report what we saw.
# We don't use a real HEAD request because not all registries honour it;
# a GET is safer and the body is small either way.
@mcp.tool(
    name="check_version_exists",
    description=(
        "Return True if the given version of the given npm package is "
        "published, False otherwise."
    ),
)
async def check_version_exists(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(description="Exact version string, e.g. '18.2.0'."),
) -> dict:
    url = f"{NPM_REGISTRY}/{package_name}/{version}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(url)
    except httpx.RequestError as exc:
        raise ValueError(f"Could not reach npm: {exc}") from exc

    if response.status_code == 200:
        return {"package": package_name, "version": version, "exists": True}
    if response.status_code == 404:
        return {"package": package_name, "version": version, "exists": False}

    raise ValueError(
        f"Unexpected HTTP {response.status_code} from npm while checking "
        f"{package_name}@{version}."
    )


# ===========================================================================
# PACKAGE INFO TOOLS
# ===========================================================================
# A second group of tools — everything focused on describing a package rather
# than navigating its versions. These are convenient wrappers over the same
# npm Registry API: the package document (/{pkg}) exposes nearly all of this
# info, but splitting it into tight single-purpose tools lets Claude pick
# exactly what it needs without reading the whole 100KB manifest.


# ---------------------------------------------------------------------------
# TOOL 7: get_package_info
# ---------------------------------------------------------------------------
# Full high-level metadata card for a package. Combines the package-level
# document with the latest version's manifest so we get both long-term info
# (maintainers, created date) and current info (description, license).
@mcp.tool(
    name="get_package_info",
    description=(
        "Full metadata overview of an npm package: name, description, author, "
        "license, homepage, maintainers, creation date."
    ),
)
async def get_package_info(
    package_name: str = Field(description="Exact npm package name."),
) -> dict:
    # Fetch both the root doc (long-term info) and the latest manifest
    # (current display fields). Doing both calls in parallel would be nicer
    # but we keep it sequential here for readability.
    pkg = await _fetch_json(
        f"{NPM_REGISTRY}/{package_name}",
        not_found_msg=f"npm package '{package_name}' was not found.",
    )
    latest = pkg.get("dist-tags", {}).get("latest")
    current = pkg.get("versions", {}).get(latest, {}) if latest else {}

    # "time" holds publish timestamps keyed by version; "created" is the very
    # first publish and "modified" is the most recent change of any kind.
    time = pkg.get("time", {})

    return {
        "name":        pkg.get("name", package_name),
        "description": current.get("description") or pkg.get("description", ""),
        "latest":      latest,
        "author":      current.get("author") or pkg.get("author"),
        "license":     current.get("license") or pkg.get("license", ""),
        "homepage":    current.get("homepage") or pkg.get("homepage", ""),
        "maintainers": [m.get("name") for m in pkg.get("maintainers", [])],
        "created":     time.get("created"),
        "modified":    time.get("modified"),
    }


# ---------------------------------------------------------------------------
# TOOL 8: get_package_readme
# ---------------------------------------------------------------------------
# npm packages ship their README markdown inside the registry document. The
# root /{pkg} response carries the README of the latest version in a top-level
# "readme" field.
@mcp.tool(
    name="get_package_readme",
    description=(
        "Fetch the README content (markdown) for an npm package. "
        "Large READMEs are truncated to 20,000 characters."
    ),
)
async def get_package_readme(
    package_name: str = Field(description="Exact npm package name."),
) -> dict:
    pkg = await _fetch_json(
        f"{NPM_REGISTRY}/{package_name}",
        not_found_msg=f"npm package '{package_name}' was not found.",
    )
    readme = pkg.get("readme", "")
    return {
        "name":      pkg.get("name", package_name),
        "has_readme": bool(readme),
        "truncated": len(readme) > 20_000,
        "content":   readme[:20_000],
    }


# ---------------------------------------------------------------------------
# TOOL 9: get_package_keywords
# ---------------------------------------------------------------------------
# Keywords are author-provided tags that help npm search surface the package.
# They live in the manifest (package.json > keywords). We read them from the
# latest version's manifest so the list reflects the current package intent.
@mcp.tool(
    name="get_package_keywords",
    description=(
        "List the keywords / tags declared by an npm package (from its "
        "package.json)."
    ),
)
async def get_package_keywords(
    package_name: str = Field(description="Exact npm package name."),
) -> dict:
    data = await _fetch_json(
        f"{NPM_REGISTRY}/{package_name}/latest",
        not_found_msg=f"npm package '{package_name}' was not found.",
    )
    keywords = data.get("keywords", []) or []
    return {
        "name":     data.get("name", package_name),
        "version":  data.get("version"),
        "count":    len(keywords),
        "keywords": keywords,
    }


# ---------------------------------------------------------------------------
# TOOL 10: get_package_repository
# ---------------------------------------------------------------------------
# The repository field in package.json tells consumers where the source lives.
# It's usually a GitHub URL but can be any VCS link. We also derive a simple
# "github_slug" (owner/repo) when we can, because that's what most downstream
# tooling actually wants.
@mcp.tool(
    name="get_package_repository",
    description=(
        "Get the source repository URL declared by an npm package, including "
        "a parsed GitHub owner/repo slug when available."
    ),
)
async def get_package_repository(
    package_name: str = Field(description="Exact npm package name."),
) -> dict:
    data = await _fetch_json(
        f"{NPM_REGISTRY}/{package_name}/latest",
        not_found_msg=f"npm package '{package_name}' was not found.",
    )
    repository = data.get("repository")
    return {
        "name":        data.get("name", package_name),
        "repository":  repository,
        "github_slug": _parse_github_slug(repository),
    }


# ---------------------------------------------------------------------------
# TOOL 11: get_package_homepage
# ---------------------------------------------------------------------------
# Some packages declare a dedicated docs/marketing homepage (e.g. react.dev),
# others fall back to the GitHub repo URL. We surface both when we can so the
# caller picks the most useful one.
@mcp.tool(
    name="get_package_homepage",
    description=(
        "Get the homepage / docs URL for an npm package, plus the npm page "
        "URL as a fallback."
    ),
)
async def get_package_homepage(
    package_name: str = Field(description="Exact npm package name."),
) -> dict:
    data = await _fetch_json(
        f"{NPM_REGISTRY}/{package_name}/latest",
        not_found_msg=f"npm package '{package_name}' was not found.",
    )
    return {
        "name":     data.get("name", package_name),
        "homepage": data.get("homepage", ""),
        "npm_url":  f"https://www.npmjs.com/package/{data.get('name', package_name)}",
    }


# ---------------------------------------------------------------------------
# TOOL 12: get_package_license
# ---------------------------------------------------------------------------
# License info can appear as a string ("MIT"), an SPDX expression, or — in
# older packages — an array of license objects. We return whatever the
# manifest provides without interpreting it; Claude is good at reasoning
# about license strings.
@mcp.tool(
    name="get_package_license",
    description="Get the license declared by an npm package (latest version).",
)
async def get_package_license(
    package_name: str = Field(description="Exact npm package name."),
) -> dict:
    data = await _fetch_json(
        f"{NPM_REGISTRY}/{package_name}/latest",
        not_found_msg=f"npm package '{package_name}' was not found.",
    )
    return {
        "name":     data.get("name", package_name),
        "version":  data.get("version"),
        "license":  data.get("license", ""),
        "licenses": data.get("licenses"),  # legacy array form, if present
    }


# ---------------------------------------------------------------------------
# TOOL 13: get_package_size
# ---------------------------------------------------------------------------
# Every published version carries a "dist" block with the tarball URL, a
# sha512 integrity hash, AND the unpacked size / file count (populated by
# modern publishes). Useful for bundle-size audits and "is this dep cheap?"
# checks.
@mcp.tool(
    name="get_package_size",
    description=(
        "Get the unpacked size (bytes + human-readable) and total file count "
        "for a specific version of an npm package."
    ),
)
async def get_package_size(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        default="",
        description="Exact version, e.g. '18.2.0'. Leave empty for the latest version.",
    ),
) -> dict:
    endpoint = f"{NPM_REGISTRY}/{package_name}/{version or 'latest'}"
    data = await _fetch_json(
        endpoint,
        not_found_msg=(
            f"Version '{version or 'latest'}' of '{package_name}' was not found."
        ),
    )
    dist = data.get("dist", {}) or {}
    size_bytes = dist.get("unpackedSize")

    # Build a human-friendly size string. npm reports bytes; we format to
    # KB / MB to match what most devs expect to see.
    if isinstance(size_bytes, int):
        if size_bytes >= 1_000_000:
            human = f"{size_bytes / 1_000_000:.2f} MB"
        elif size_bytes >= 1_000:
            human = f"{size_bytes / 1_000:.2f} KB"
        else:
            human = f"{size_bytes} B"
    else:
        human = "unknown"

    return {
        "name":            data.get("name", package_name),
        "version":         data.get("version"),
        "unpacked_bytes":  size_bytes,
        "unpacked_human":  human,
        "file_count":      dist.get("fileCount"),
        "tarball":         dist.get("tarball"),
    }


# ===========================================================================
# DEPENDENCY TOOLS
# ===========================================================================
# Every npm package declares three kinds of dependencies in its manifest:
#   - dependencies       → installed in prod
#   - devDependencies    → only needed while developing the package
#   - peerDependencies   → the host app MUST provide these (e.g. react-dom
#                          requires a compatible react to be installed)
# The tools below expose each group directly and add two higher-level tools
# for walking the transitive tree and checking peer compatibility.


# ---------------------------------------------------------------------------
# Shared helper: fetch a single version's manifest
# ---------------------------------------------------------------------------
# A thin wrapper so every dependency tool resolves `version` the same way
# (empty -> latest) and surfaces the same 404 message format.
async def _fetch_manifest(package_name: str, version: str = "") -> dict:
    return await _fetch_json(
        f"{NPM_REGISTRY}/{package_name}/{version or 'latest'}",
        not_found_msg=(
            f"Version '{version or 'latest'}' of '{package_name}' was not found."
        ),
    )


# ---------------------------------------------------------------------------
# TOOL 14: get_dependencies
# ---------------------------------------------------------------------------
# The main prod-time dependency list. Keys are package names, values are the
# declared version ranges (e.g. "react": "^18.0.0").
@mcp.tool(
    name="get_dependencies",
    description=(
        "List the runtime dependencies of a specific version of an npm "
        "package (the `dependencies` field of package.json)."
    ),
)
async def get_dependencies(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        default="",
        description="Exact version string. Leave empty for the latest version.",
    ),
) -> dict:
    data = await _fetch_manifest(package_name, version)
    deps = data.get("dependencies", {}) or {}
    return {
        "name":    data.get("name", package_name),
        "version": data.get("version"),
        "count":   len(deps),
        "dependencies": deps,
    }


# ---------------------------------------------------------------------------
# TOOL 15: get_peer_dependencies
# ---------------------------------------------------------------------------
# peerDependencies are packages the consumer MUST install themselves. A UI
# library declaring `"react": ">=16.8"` means "I don't ship react; you must
# provide a compatible one in your app".
@mcp.tool(
    name="get_peer_dependencies",
    description=(
        "List the peerDependencies of a specific version of an npm package "
        "— packages the host app is expected to provide."
    ),
)
async def get_peer_dependencies(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        default="",
        description="Exact version string. Leave empty for the latest version.",
    ),
) -> dict:
    data = await _fetch_manifest(package_name, version)
    peers = data.get("peerDependencies", {}) or {}
    meta = data.get("peerDependenciesMeta", {}) or {}
    return {
        "name":    data.get("name", package_name),
        "version": data.get("version"),
        "count":   len(peers),
        "peer_dependencies": peers,
        # peerDependenciesMeta marks peers as optional when present.
        "peer_meta": meta,
    }


# ---------------------------------------------------------------------------
# TOOL 16: get_dev_dependencies
# ---------------------------------------------------------------------------
# Only relevant when the caller cares about *building* the package (tests,
# linters, bundlers). Not installed when you just `npm install <pkg>`.
@mcp.tool(
    name="get_dev_dependencies",
    description=(
        "List the devDependencies of a specific version of an npm package "
        "— test/build-time deps that aren't installed by consumers."
    ),
)
async def get_dev_dependencies(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        default="",
        description="Exact version string. Leave empty for the latest version.",
    ),
) -> dict:
    data = await _fetch_manifest(package_name, version)
    dev = data.get("devDependencies", {}) or {}
    return {
        "name":    data.get("name", package_name),
        "version": data.get("version"),
        "count":   len(dev),
        "dev_dependencies": dev,
    }


# ---------------------------------------------------------------------------
# TOOL 17: get_dependency_tree
# ---------------------------------------------------------------------------
# Walks the transitive prod-dependency graph. Because every node is a separate
# HTTP call, we:
#   - fetch a node's direct deps in PARALLEL with asyncio.gather
#   - cap the recursion with `max_depth` (default 2) so we don't explode
#   - de-duplicate: if a package is already resolved in the tree, we reuse
#     the result instead of re-fetching (avoids cycles and spares the registry)
@mcp.tool(
    name="get_dependency_tree",
    description=(
        "Resolve the full transitive dependency tree for a package. Each node "
        "shows its version and its direct dependencies. Depth is capped to "
        "prevent huge trees — increase `max_depth` cautiously."
    ),
)
async def get_dependency_tree(
    package_name: str = Field(description="Exact npm package name (root)."),
    version: str = Field(
        default="",
        description="Exact version. Leave empty for the latest version.",
    ),
    max_depth: int = Field(
        default=2,
        description="Maximum recursion depth. 0 = just the root; 2 = root + 2 levels.",
    ),
) -> dict:
    # Cache maps (name, resolved_range) -> already-built subtree so we only
    # resolve each unique dep once per call.
    cache: dict[str, dict] = {}

    async def walk(name: str, version_range: str, depth: int) -> dict:
        # Resolve the range to a concrete manifest. For simplicity we just
        # strip common range prefixes (^, ~, >=) and pass the rest to npm.
        # npm is permissive: passing "^18.0.0" to /{pkg}/{version} returns
        # the best-matching version.
        resolved_key = f"{name}@{version_range}"
        if resolved_key in cache:
            return {**cache[resolved_key], "deduped": True}

        try:
            manifest = await _fetch_manifest(name, version_range.lstrip("^~>=< "))
        except ValueError as exc:
            # Don't blow up the whole tree for one missing node.
            return {
                "name": name,
                "requested": version_range,
                "error": str(exc),
            }

        node: dict = {
            "name": manifest.get("name", name),
            "version": manifest.get("version"),
            "requested": version_range,
        }

        direct_deps = manifest.get("dependencies", {}) or {}
        if depth <= 0 or not direct_deps:
            node["dependencies"] = {}
            cache[resolved_key] = node
            return node

        # Fan out in parallel — this is the hot loop that keeps the tree fast.
        children = await asyncio.gather(
            *(walk(dep_name, dep_range, depth - 1)
              for dep_name, dep_range in direct_deps.items()),
            return_exceptions=False,
        )
        node["dependencies"] = {c.get("name", n): c
                                for c, n in zip(children, direct_deps)}
        cache[resolved_key] = node
        return node

    # Clamp max_depth to a reasonable range so a typo doesn't hammer npm.
    safe_depth = max(0, min(int(max_depth), 4))
    root = await walk(package_name, version or "latest", safe_depth)
    return {"max_depth": safe_depth, "tree": root}


# ---------------------------------------------------------------------------
# TOOL 18: check_peer_compatibility
# ---------------------------------------------------------------------------
# Given a package and an (optional) map of what the caller has installed,
# report each peer dep's range and whether the installed version satisfies
# it. We implement a tiny semver matcher here covering the ranges that cover
# ~90% of real-world peerDependencies: exact, ^, ~, >=, <=, >, <, *.
def _parse_semver(v: str) -> tuple[int, int, int] | None:
    """Parse a plain 'x.y.z' (ignore pre-release/build for this check)."""
    try:
        core = v.lstrip("v").split("-", 1)[0].split("+", 1)[0]
        parts = core.split(".")
        if len(parts) < 3:
            parts += ["0"] * (3 - len(parts))
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, AttributeError):
        return None


def _satisfies(installed: str, npm_range: str) -> str:
    """
    Check if `installed` satisfies an npm `npm_range`.
    Returns: "yes" | "no" | "unknown" (for ranges we don't fully support).
    """
    inst = _parse_semver(installed)
    npm_range = (npm_range or "").strip()
    if not npm_range or npm_range == "*":
        return "yes"
    if inst is None:
        return "unknown"

    # Handle compound ranges ("||" = OR) by recursion.
    if "||" in npm_range:
        return "yes" if any(
            _satisfies(installed, part.strip()) == "yes"
            for part in npm_range.split("||")
        ) else "unknown"

    # Caret: compatible with the declared major (or minor if major==0).
    if npm_range.startswith("^"):
        tgt = _parse_semver(npm_range[1:])
        if not tgt:
            return "unknown"
        if tgt[0] > 0:
            return "yes" if inst[0] == tgt[0] and inst >= tgt else "no"
        if tgt[1] > 0:
            return "yes" if inst[:2] == tgt[:2] and inst >= tgt else "no"
        return "yes" if inst == tgt else "no"

    # Tilde: allows patch bumps.
    if npm_range.startswith("~"):
        tgt = _parse_semver(npm_range[1:])
        if not tgt:
            return "unknown"
        return "yes" if inst[:2] == tgt[:2] and inst >= tgt else "no"

    # Simple comparators.
    for op in (">=", "<=", ">", "<", "="):
        if npm_range.startswith(op):
            tgt = _parse_semver(npm_range[len(op):].strip())
            if not tgt:
                return "unknown"
            if op == ">=":
                return "yes" if inst >= tgt else "no"
            if op == "<=":
                return "yes" if inst <= tgt else "no"
            if op == ">":
                return "yes" if inst > tgt else "no"
            if op == "<":
                return "yes" if inst < tgt else "no"
            if op == "=":
                return "yes" if inst == tgt else "no"

    # Plain "1.2.3" is treated as an exact match.
    tgt = _parse_semver(npm_range)
    if tgt:
        return "yes" if inst == tgt else "no"
    return "unknown"


@mcp.tool(
    name="check_peer_compatibility",
    description=(
        "Check whether a package's peerDependencies are satisfied by a given "
        "set of installed versions. Pass `installed` as a map of "
        "{package_name: installed_version}. Returns a per-peer compatibility "
        "report (yes / no / unknown)."
    ),
)
async def check_peer_compatibility(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        default="",
        description="Exact version. Leave empty for the latest version.",
    ),
    installed: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map of {peer_name: installed_version} describing what you have. "
            "Peers not listed are reported as 'missing'."
        ),
    ),
) -> dict:
    data = await _fetch_manifest(package_name, version)
    peers = data.get("peerDependencies", {}) or {}
    meta = data.get("peerDependenciesMeta", {}) or {}

    report = []
    for peer_name, required_range in peers.items():
        peer_meta = meta.get(peer_name, {}) or {}
        installed_version = installed.get(peer_name)
        if installed_version is None:
            status = "missing-optional" if peer_meta.get("optional") else "missing"
        else:
            status = _satisfies(installed_version, required_range)
        report.append({
            "peer": peer_name,
            "required": required_range,
            "installed": installed_version,
            "optional": bool(peer_meta.get("optional")),
            "satisfied": status,
        })

    all_ok = all(r["satisfied"] == "yes" or r["satisfied"] == "missing-optional"
                 for r in report)
    return {
        "name": data.get("name", package_name),
        "version": data.get("version"),
        "all_ok": all_ok,
        "peers_checked": len(report),
        "report": report,
    }


# ===========================================================================
# SECURITY & HEALTH TOOLS
# ===========================================================================
# These answer the question "is this package safe and alive?":
#   check_vulnerabilities     - known CVEs (via OSV database)
#   get_deprecation_status    - is a package/version deprecated?
#   check_maintainer_activity - last publish date, abandonment heuristics
#   get_download_stats        - weekly / monthly download counts
#   check_typosquat_risk      - flag names that look like common packages


# ---------------------------------------------------------------------------
# Top-50 popular npm packages — used by check_typosquat_risk.
# Kept short & hand-picked: chosen because typos on these cause real-world
# supply-chain attacks (e.g. "cross-env" vs "crossenv" historically).
# ---------------------------------------------------------------------------
_TOP_PACKAGES = [
    "react", "react-dom", "lodash", "axios", "express", "vue", "next",
    "typescript", "webpack", "eslint", "prettier", "jest", "babel",
    "rollup", "vite", "nestjs", "svelte", "angular", "jquery", "moment",
    "chalk", "commander", "cross-env", "dotenv", "fs-extra", "glob",
    "mocha", "node-fetch", "nodemon", "request", "rimraf", "semver",
    "underscore", "uuid", "yargs", "async", "bluebird", "colors", "debug",
    "inquirer", "minimist", "ora", "path", "redux", "rxjs", "socket.io",
    "tslib", "winston", "ws", "zod",
]


def _levenshtein(a: str, b: str) -> int:
    """Tiny iterative Levenshtein — good enough for short package names."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # Classic DP, O(len(a) * len(b)) — trivial for <~40 char npm names.
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr[j] = min(
                curr[j - 1] + 1,      # insertion
                prev[j] + 1,          # deletion
                prev[j - 1] + cost,   # substitution
            )
        prev = curr
    return prev[-1]


# ---------------------------------------------------------------------------
# TOOL 19: check_vulnerabilities
# ---------------------------------------------------------------------------
# Uses the public OSV.dev API (maintained by Google). Covers the npm ecosystem
# and aggregates advisories from GitHub, npm audit, and others. No auth needed.
@mcp.tool(
    name="check_vulnerabilities",
    description=(
        "Check an npm package/version for known vulnerabilities (CVEs) via "
        "the OSV.dev database. Returns a summary plus per-advisory details."
    ),
)
async def check_vulnerabilities(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        default="",
        description="Exact version to check. Leave empty for the latest version.",
    ),
) -> dict:
    # Resolve "latest" up front so the OSV query is always version-specific.
    resolved_version = version
    if not resolved_version:
        latest = await _fetch_json(
            f"{NPM_REGISTRY}/{package_name}/latest",
            not_found_msg=f"npm package '{package_name}' was not found.",
        )
        resolved_version = latest.get("version", "")

    payload = {
        "package": {"name": package_name, "ecosystem": "npm"},
        "version": resolved_version,
    }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post("https://api.osv.dev/v1/query", json=payload)
    except httpx.RequestError as exc:
        raise ValueError(f"Could not reach OSV.dev: {exc}") from exc

    if response.status_code >= 400:
        raise ValueError(
            f"OSV returned HTTP {response.status_code} for {package_name}@{resolved_version}."
        )

    data = response.json()
    vulns = data.get("vulns", []) or []

    # Summarise each advisory — full OSV records are huge, so we trim.
    advisories = []
    for v in vulns:
        severity_list = v.get("severity", []) or []
        severity = severity_list[0].get("score", "") if severity_list else ""
        advisories.append({
            "id": v.get("id"),
            "summary": v.get("summary", ""),
            "severity": severity,
            "aliases": v.get("aliases", []),
            "published": v.get("published"),
            "modified": v.get("modified"),
            "references": [r.get("url") for r in v.get("references", [])[:3]],
        })

    return {
        "package": package_name,
        "version": resolved_version,
        "vulnerable": bool(advisories),
        "count": len(advisories),
        "advisories": advisories,
        "source": "osv.dev",
    }


# ---------------------------------------------------------------------------
# TOOL 20: get_deprecation_status
# ---------------------------------------------------------------------------
# npm marks deprecations in the manifest's top-level "deprecated" field. For
# a version-specific check we read the version manifest; for whole-package
# status we scan all versions and report how many carry a deprecation string.
@mcp.tool(
    name="get_deprecation_status",
    description=(
        "Check if an npm package or a specific version is deprecated. "
        "Returns the deprecation message when present."
    ),
)
async def get_deprecation_status(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        default="",
        description="Exact version. Leave empty to scan ALL versions of the package.",
    ),
) -> dict:
    if version:
        # Version-specific: single manifest fetch.
        data = await _fetch_manifest(package_name, version)
        msg = data.get("deprecated")
        return {
            "package": package_name,
            "version": data.get("version", version),
            "deprecated": bool(msg),
            "message": msg or "",
        }

    # Whole-package scan: fetch the full package doc and inspect every version.
    pkg = await _fetch_json(
        f"{NPM_REGISTRY}/{package_name}",
        not_found_msg=f"npm package '{package_name}' was not found.",
    )
    versions = pkg.get("versions", {}) or {}
    deprecated_versions = {
        v: manifest.get("deprecated")
        for v, manifest in versions.items()
        if manifest.get("deprecated")
    }
    latest_tag = pkg.get("dist-tags", {}).get("latest")
    latest_msg = versions.get(latest_tag, {}).get("deprecated", "") if latest_tag else ""

    return {
        "package": package_name,
        "total_versions": len(versions),
        "deprecated_count": len(deprecated_versions),
        "latest_version": latest_tag,
        "latest_deprecated": bool(latest_msg),
        "latest_message": latest_msg or "",
        # Only surface a handful to keep the payload small.
        "sample_deprecated": dict(list(deprecated_versions.items())[:10]),
    }


# ---------------------------------------------------------------------------
# TOOL 21: check_maintainer_activity
# ---------------------------------------------------------------------------
# Uses the "time" field from the package document: it maps version -> publish
# timestamp, plus "created" (first publish) and "modified" (any change).
# We compute:
#   - days since last publish
#   - total publish count
#   - a heuristic "abandoned" flag (>= 730 days since last publish)
@mcp.tool(
    name="check_maintainer_activity",
    description=(
        "Assess if an npm package is actively maintained. Reports last publish "
        "date, total publish count, average cadence, and an abandonment heuristic."
    ),
)
async def check_maintainer_activity(
    package_name: str = Field(description="Exact npm package name."),
) -> dict:
    from datetime import datetime, timezone

    pkg = await _fetch_json(
        f"{NPM_REGISTRY}/{package_name}",
        not_found_msg=f"npm package '{package_name}' was not found.",
    )
    time = pkg.get("time", {}) or {}
    created = time.get("created")
    modified = time.get("modified")

    # Filter out the non-version keys ("created", "modified") to get pure
    # version -> timestamp pairs.
    version_times = {k: v for k, v in time.items() if k not in ("created", "modified")}
    publish_count = len(version_times)

    def _parse(ts: str | None) -> datetime | None:
        if not ts:
            return None
        try:
            # npm timestamps are ISO-8601 UTC with "Z"; fromisoformat wants "+00:00".
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None

    now = datetime.now(timezone.utc)
    last_publish = _parse(modified) or _parse(created)
    first_publish = _parse(created)

    days_since_last = (
        (now - last_publish).days if last_publish else None
    )
    total_lifetime_days = (
        (now - first_publish).days if first_publish else None
    )
    avg_days_between = (
        round(total_lifetime_days / publish_count, 1)
        if total_lifetime_days and publish_count > 1
        else None
    )

    # Simple heuristic: 2 years without any publish = likely abandoned.
    abandoned = days_since_last is not None and days_since_last >= 730

    # Status label for quick skimming.
    if days_since_last is None:
        status = "unknown"
    elif days_since_last < 90:
        status = "active"
    elif days_since_last < 365:
        status = "slowing"
    elif days_since_last < 730:
        status = "stale"
    else:
        status = "abandoned"

    return {
        "package": package_name,
        "created": created,
        "last_publish": modified,
        "days_since_last_publish": days_since_last,
        "publish_count": publish_count,
        "avg_days_between_publishes": avg_days_between,
        "maintainers": [m.get("name") for m in pkg.get("maintainers", [])],
        "status": status,
        "abandoned": abandoned,
    }


# ---------------------------------------------------------------------------
# TOOL 22: get_download_stats
# ---------------------------------------------------------------------------
# npm exposes a separate download-stats API at api.npmjs.org. Periods can be
# "last-day", "last-week", "last-month", or a custom date range. We expose the
# three common ones in one response so callers don't have to make 3 tools.
@mcp.tool(
    name="get_download_stats",
    description=(
        "Get download statistics for an npm package: day / week / month counts "
        "from the public npm download API."
    ),
)
async def get_download_stats(
    package_name: str = Field(description="Exact npm package name."),
) -> dict:
    base = "https://api.npmjs.org/downloads/point"
    periods = ["last-day", "last-week", "last-month"]

    async def _fetch_period(period: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                response = await client.get(f"{base}/{period}/{package_name}")
        except httpx.RequestError as exc:
            return {"period": period, "error": str(exc)}
        if response.status_code == 404:
            return {"period": period, "downloads": 0, "error": "not found"}
        if response.status_code >= 400:
            return {"period": period, "error": f"HTTP {response.status_code}"}
        data = response.json()
        return {
            "period": period,
            "downloads": data.get("downloads", 0),
            "start": data.get("start"),
            "end": data.get("end"),
        }

    # Fan out in parallel — three independent HTTP calls.
    results = await asyncio.gather(*(_fetch_period(p) for p in periods))
    by_period = {r["period"]: r for r in results}

    last_month = by_period.get("last-month", {}).get("downloads", 0) or 0
    # Rough "is this widely used?" heuristic.
    if last_month >= 1_000_000:
        popularity = "massive"
    elif last_month >= 100_000:
        popularity = "popular"
    elif last_month >= 10_000:
        popularity = "moderate"
    elif last_month >= 1_000:
        popularity = "niche"
    else:
        popularity = "obscure"

    return {
        "package": package_name,
        "last_day":   by_period.get("last-day", {}).get("downloads", 0),
        "last_week":  by_period.get("last-week", {}).get("downloads", 0),
        "last_month": last_month,
        "popularity": popularity,
        "source": "api.npmjs.org",
    }


# ---------------------------------------------------------------------------
# TOOL 23: check_typosquat_risk
# ---------------------------------------------------------------------------
# Typosquatting attacks exploit small misspellings of popular packages. We
# compute Levenshtein distance against a short list of high-profile names and
# flag anything within 1-2 edits that ISN'T already that package.
@mcp.tool(
    name="check_typosquat_risk",
    description=(
        "Check whether an npm package name looks suspiciously close to a "
        "popular package — a rough typosquat heuristic. Returns risk level "
        "and the nearest popular matches."
    ),
)
async def check_typosquat_risk(
    package_name: str = Field(description="Exact npm package name to evaluate."),
) -> dict:
    name = package_name.strip().lower()

    # Exact match against our popular list = not a squat, just the real thing.
    if name in _TOP_PACKAGES:
        return {
            "package": package_name,
            "risk": "none",
            "reason": "Exact match for a known popular package.",
            "matches": [],
        }

    # Compute distances — small list, so O(n * m) is fine.
    scored = sorted(
        ((pkg, _levenshtein(name, pkg)) for pkg in _TOP_PACKAGES),
        key=lambda t: t[1],
    )[:5]

    closest_name, closest_dist = scored[0]

    # Risk bands — tuned to catch the common attack patterns without crying
    # wolf on genuinely new names.
    if closest_dist == 0:
        risk = "none"
    elif closest_dist == 1:
        risk = "high"     # classic 1-char typo (e.g. "exress" for "express")
    elif closest_dist == 2:
        risk = "medium"   # plausible typo, worth a warning
    elif closest_dist <= 4:
        risk = "low"
    else:
        risk = "none"

    reason = (
        f"Name is {closest_dist} character edits away from '{closest_name}' "
        "— review before installing."
        if risk in ("high", "medium")
        else "No close matches to known popular packages."
    )

    return {
        "package": package_name,
        "risk": risk,
        "reason": reason,
        "closest_popular": closest_name,
        "edit_distance": closest_dist,
        "matches": [
            {"name": n, "distance": d} for n, d in scored if d <= 4
        ],
    }


# ===========================================================================
# COMPATIBILITY & UPDATE TOOLS
# ===========================================================================
# These answer upgrade / update planning questions:
#   check_node_compatibility - what Node versions does this package support?
#   compare_versions         - what changed between v1 and v2 of a package?
#   get_breaking_changes     - which transitive deps had a major bump?
#   resolve_semver           - resolve "^18.0.0" to a concrete version
#   check_outdated           - bulk "is each of these packages outdated?"


# ---------------------------------------------------------------------------
# TOOL 24: check_node_compatibility
# ---------------------------------------------------------------------------
# Reads the manifest's "engines" field — commonly {"node": ">=18"}, sometimes
# also carries "npm" or "yarn" constraints. Tells Claude whether a package
# will even install on the user's runtime.
@mcp.tool(
    name="check_node_compatibility",
    description=(
        "Return the engines field (node / npm / yarn constraints) declared "
        "by an npm package for a specific version, or the latest version."
    ),
)
async def check_node_compatibility(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        default="",
        description="Exact version. Leave empty for the latest version.",
    ),
) -> dict:
    data = await _fetch_manifest(package_name, version)
    engines = data.get("engines", {}) or {}
    return {
        "package": data.get("name", package_name),
        "version": data.get("version"),
        "engines": engines,
        "node":  engines.get("node", ""),
        "npm":   engines.get("npm", ""),
        "yarn":  engines.get("yarn", ""),
        # Convenience flag — callers often just want to know if ANY node range
        # is declared (absence is treated by npm as "any version allowed").
        "has_node_constraint": bool(engines.get("node")),
    }


# ---------------------------------------------------------------------------
# TOOL 25: compare_versions
# ---------------------------------------------------------------------------
# Useful for upgrade planning. Fetches both version manifests in parallel and
# diffs their dependencies / devDependencies / peerDependencies — reports
# added, removed, and range-changed entries.
def _diff_maps(old: dict, new: dict) -> dict:
    """Diff two {name: range} maps — returns added, removed, changed."""
    old_set, new_set = set(old), set(new)
    added   = {k: new[k] for k in new_set - old_set}
    removed = {k: old[k] for k in old_set - new_set}
    changed = {
        k: {"from": old[k], "to": new[k]}
        for k in old_set & new_set
        if old[k] != new[k]
    }
    return {"added": added, "removed": removed, "changed": changed}


@mcp.tool(
    name="compare_versions",
    description=(
        "Diff two versions of an npm package: which dependencies, "
        "devDependencies, and peerDependencies were added, removed, or "
        "range-changed between them."
    ),
)
async def compare_versions(
    package_name: str = Field(description="Exact npm package name."),
    from_version: str = Field(description="The older version, e.g. '18.0.0'."),
    to_version:   str = Field(description="The newer version, e.g. '19.0.0'."),
) -> dict:
    # Parallel fetch — keeps the tool snappy for large manifests.
    old_manifest, new_manifest = await asyncio.gather(
        _fetch_manifest(package_name, from_version),
        _fetch_manifest(package_name, to_version),
    )

    return {
        "package": package_name,
        "from": old_manifest.get("version", from_version),
        "to":   new_manifest.get("version", to_version),
        "dependencies":     _diff_maps(
            old_manifest.get("dependencies", {}) or {},
            new_manifest.get("dependencies", {}) or {},
        ),
        "dev_dependencies": _diff_maps(
            old_manifest.get("devDependencies", {}) or {},
            new_manifest.get("devDependencies", {}) or {},
        ),
        "peer_dependencies": _diff_maps(
            old_manifest.get("peerDependencies", {}) or {},
            new_manifest.get("peerDependencies", {}) or {},
        ),
        # Engines changes are a common "breaking" signal on their own.
        "engines": {
            "from": old_manifest.get("engines", {}),
            "to":   new_manifest.get("engines", {}),
        },
    }


# ---------------------------------------------------------------------------
# TOOL 26: get_breaking_changes
# ---------------------------------------------------------------------------
# Focused on dependency bumps rather than the package's own code. We diff
# the two versions' dependency maps and flag the ones whose declared range
# bumped across a major version boundary (e.g. ^1.x -> ^2.x). Returns a
# per-dep report plus a summary count — gives Claude a clear signal of what
# a consumer upgrading from vA to vB will need to reconcile.
def _extract_major(semver_range: str) -> int | None:
    """Best-effort: pull the major-version digit out of an npm range."""
    if not semver_range:
        return None
    # Strip common operators, then take the first number before a dot.
    s = semver_range.strip().lstrip("^~>=<v ")
    # "1.2.3", "1", "18.x" — all work; bail out for "*", "latest", git URLs.
    head = s.split(".", 1)[0].split("-", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


@mcp.tool(
    name="get_breaking_changes",
    description=(
        "Compare two versions of an npm package and flag dependencies whose "
        "declared range bumped across a MAJOR version boundary — the most "
        "common source of breaking changes when upgrading."
    ),
)
async def get_breaking_changes(
    package_name: str = Field(description="Exact npm package name."),
    from_version: str = Field(description="The older version."),
    to_version:   str = Field(description="The newer version."),
) -> dict:
    old_manifest, new_manifest = await asyncio.gather(
        _fetch_manifest(package_name, from_version),
        _fetch_manifest(package_name, to_version),
    )
    old_deps = old_manifest.get("dependencies", {}) or {}
    new_deps = new_manifest.get("dependencies", {}) or {}
    old_peers = old_manifest.get("peerDependencies", {}) or {}
    new_peers = new_manifest.get("peerDependencies", {}) or {}

    def _scan(old: dict, new: dict, kind: str) -> list[dict]:
        out = []
        for name in set(old) & set(new):
            old_major = _extract_major(old[name])
            new_major = _extract_major(new[name])
            if (old_major is not None and new_major is not None
                    and old_major != new_major):
                out.append({
                    "kind": kind,
                    "dependency": name,
                    "from": old[name],
                    "to":   new[name],
                    "from_major": old_major,
                    "to_major":   new_major,
                })
        return out

    # Major bumps on deps are almost always breaking for consumers. Peer
    # bumps are even more important — they force the host app to upgrade too.
    dep_bumps  = _scan(old_deps,  new_deps,  "dependency")
    peer_bumps = _scan(old_peers, new_peers, "peerDependency")

    # Engine changes are a separate "breaking" axis worth surfacing.
    old_node = (old_manifest.get("engines", {}) or {}).get("node", "")
    new_node = (new_manifest.get("engines", {}) or {}).get("node", "")
    engine_change = (
        {"from": old_node, "to": new_node}
        if old_node != new_node and (old_node or new_node)
        else None
    )

    all_bumps = dep_bumps + peer_bumps
    return {
        "package": package_name,
        "from": old_manifest.get("version", from_version),
        "to":   new_manifest.get("version", to_version),
        "breaking_major_bumps": all_bumps,
        "total_major_bumps":    len(all_bumps),
        "node_engine_change":   engine_change,
        "likely_breaking":      bool(all_bumps or engine_change),
    }


# ---------------------------------------------------------------------------
# TOOL 27: resolve_semver
# ---------------------------------------------------------------------------
# Given an npm range (e.g. "^18.0.0"), find the HIGHEST published version
# that satisfies it. Re-uses the tiny semver matcher from check_peer_compat
# so we don't add a dependency.
@mcp.tool(
    name="resolve_semver",
    description=(
        "Resolve an npm semver range (e.g. '^18.0.0', '~4.17.20', '>=2 <3') "
        "to the highest published version of the package that satisfies it."
    ),
)
async def resolve_semver(
    package_name: str = Field(description="Exact npm package name."),
    version_range: str = Field(
        description="Any npm-style range: '^1.2.3', '~1.2', '>=1 <2', '1.x', '*'."
    ),
) -> dict:
    # Pull every published version so we can scan locally.
    pkg = await _fetch_json(
        f"{NPM_REGISTRY}/{package_name}",
        not_found_msg=f"npm package '{package_name}' was not found.",
    )
    versions = list((pkg.get("versions", {}) or {}).keys())
    if not versions:
        raise ValueError(f"'{package_name}' has no published versions.")

    # Walk in order and collect matches; then pick the highest by semver tuple.
    matches = [v for v in versions if _satisfies(v, version_range) == "yes"]
    if not matches:
        return {
            "package": package_name,
            "range": version_range,
            "resolved": None,
            "matched_count": 0,
            "note": "No published version matches this range.",
        }

    def _key(v: str) -> tuple:
        parsed = _parse_semver(v)
        return parsed if parsed is not None else (0, 0, 0)

    resolved = max(matches, key=_key)
    return {
        "package": package_name,
        "range": version_range,
        "resolved": resolved,
        "matched_count": len(matches),
        # Show the tail of the matched list — useful for "what else would work".
        "candidates": matches[-10:],
    }


# ---------------------------------------------------------------------------
# TOOL 28: check_outdated
# ---------------------------------------------------------------------------
# Bulk "npm outdated" style check — given a map of {name: installed_version},
# fetch each package's "latest" in parallel and flag the ones that have a
# newer release. Cheaper and faster than running npm outdated in a sandbox.
@mcp.tool(
    name="check_outdated",
    description=(
        "Given a map of {package_name: installed_version}, report which have "
        "newer versions on npm. Returns per-package status plus a summary."
    ),
)
async def check_outdated(
    packages: dict[str, str] = Field(
        description="Map of {package_name: installed_version} to check."
    ),
) -> dict:
    if not packages:
        return {"checked": 0, "outdated": [], "up_to_date": [], "errors": []}

    async def _check(name: str, installed: str) -> dict:
        try:
            data = await _fetch_json(
                f"{NPM_REGISTRY}/{name}/latest",
                not_found_msg=f"npm package '{name}' was not found.",
            )
        except ValueError as exc:
            return {"name": name, "installed": installed, "error": str(exc)}

        latest = data.get("version", "")
        installed_t = _parse_semver(installed)
        latest_t    = _parse_semver(latest)

        # If either side doesn't parse, fall back to a plain string compare.
        if installed_t is None or latest_t is None:
            is_outdated = installed != latest
        else:
            is_outdated = latest_t > installed_t

        # Classify the magnitude of the gap so Claude can prioritise.
        gap = "none"
        if installed_t and latest_t:
            if latest_t[0] > installed_t[0]:
                gap = "major"
            elif latest_t[1] > installed_t[1]:
                gap = "minor"
            elif latest_t[2] > installed_t[2]:
                gap = "patch"

        return {
            "name": name,
            "installed": installed,
            "latest": latest,
            "outdated": is_outdated,
            "gap": gap,
        }

    # Parallel fan-out — much faster than sequential for big lock files.
    results = await asyncio.gather(
        *(_check(name, v) for name, v in packages.items())
    )

    outdated   = [r for r in results if r.get("outdated")]
    up_to_date = [r for r in results if r.get("outdated") is False]
    errors     = [r for r in results if "error" in r]

    return {
        "checked": len(results),
        "outdated_count":   len(outdated),
        "up_to_date_count": len(up_to_date),
        "error_count":      len(errors),
        "outdated":   outdated,
        "up_to_date": up_to_date,
        "errors":     errors,
    }


# ===========================================================================
# SEARCH & DISCOVERY TOOLS
# ===========================================================================
# Wraps npm's public search endpoint (https://registry.npmjs.org/-/v1/search)
# for finding packages rather than looking them up by name. Lets Claude
# answer questions like "what http client libraries exist?" or "what packages
# does @babel publish?" without you telling it the exact name.


# ---------------------------------------------------------------------------
# Shared helper for the search endpoint
# ---------------------------------------------------------------------------
# npm's search returns a rich structure: each hit has { package, score, ... }
# where `package` is the manifest-flavoured summary and `score` is npm's own
# quality/popularity/maintenance ranking. We trim each hit to a stable shape.
NPM_SEARCH = f"{NPM_REGISTRY}/-/v1/search"


def _summarise_search_hit(hit: dict) -> dict:
    pkg = hit.get("package", {}) or {}
    score = hit.get("score", {}) or {}
    detail = score.get("detail", {}) or {}
    links = pkg.get("links", {}) or {}
    return {
        "name":        pkg.get("name"),
        "version":     pkg.get("version"),
        "description": pkg.get("description", ""),
        "keywords":    pkg.get("keywords", []) or [],
        "date":        pkg.get("date"),
        "publisher":   (pkg.get("publisher") or {}).get("username"),
        "npm_url":     links.get("npm"),
        "homepage":    links.get("homepage"),
        "repository":  links.get("repository"),
        # Normalise npm's 0-1 scores — useful for Claude to rank results.
        "score":       round(hit.get("searchScore", 0) or 0, 2),
        "quality":     round(detail.get("quality", 0), 2),
        "popularity":  round(detail.get("popularity", 0), 2),
        "maintenance": round(detail.get("maintenance", 0), 2),
    }


async def _npm_search(query: str, size: int = 20) -> list[dict]:
    """Call the npm search API and return a list of summarised hits."""
    # Clamp size — npm's endpoint caps at 250, but we want small payloads.
    safe_size = max(1, min(int(size), 50))
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(
                NPM_SEARCH,
                params={"text": query, "size": safe_size},
            )
    except httpx.RequestError as exc:
        raise ValueError(f"Could not reach npm search: {exc}") from exc

    if response.status_code >= 400:
        raise ValueError(
            f"npm search returned HTTP {response.status_code} for '{query}'."
        )

    return [_summarise_search_hit(h) for h in response.json().get("objects", [])]


# ---------------------------------------------------------------------------
# TOOL 29: search_packages
# ---------------------------------------------------------------------------
# Straightforward keyword search. npm supports "keywords:foo", "author:bar",
# "scope:@babel" qualifiers in the text, but for this tool we keep it simple:
# whatever string the caller provides becomes the free-text query.
@mcp.tool(
    name="search_packages",
    description=(
        "Search the npm Registry by free-text keyword. Returns a ranked list "
        "of packages with descriptions, scores, and metadata."
    ),
)
async def search_packages(
    query: str = Field(
        description="Search query — can be any keyword(s), e.g. 'http client' or 'react hooks'."
    ),
    limit: int = Field(
        default=20,
        description="Maximum number of results to return (1–50).",
    ),
) -> dict:
    hits = await _npm_search(query, size=limit)
    return {
        "query":   query,
        "count":   len(hits),
        "results": hits,
    }


# ---------------------------------------------------------------------------
# TOOL 30: get_similar_packages
# ---------------------------------------------------------------------------
# "Alternatives to X" — we grab the package's own keywords (the author-set
# tags) and run them as a search query, then filter out the package itself.
# Keyword-driven works well in practice because packages in the same space
# share tags like "validation", "orm", "state-management", etc.
@mcp.tool(
    name="get_similar_packages",
    description=(
        "Find alternatives to a given npm package. Uses the package's own "
        "declared keywords to search for packages in the same space."
    ),
)
async def get_similar_packages(
    package_name: str = Field(description="Exact npm package name to find alternatives for."),
    limit: int = Field(
        default=10,
        description="Maximum number of similar packages to return (1–50).",
    ),
) -> dict:
    # Pull the latest manifest's keywords.
    manifest = await _fetch_json(
        f"{NPM_REGISTRY}/{package_name}/latest",
        not_found_msg=f"npm package '{package_name}' was not found.",
    )
    keywords = manifest.get("keywords", []) or []
    if not keywords:
        return {
            "package": package_name,
            "keywords_used": [],
            "count": 0,
            "results": [],
            "note": "This package declares no keywords — cannot derive similar packages.",
        }

    # Take the top handful of keywords to keep the query tight and relevant.
    top_keywords = keywords[:5]
    query = " ".join(top_keywords)

    # Ask npm for a few extra so we can filter out the source package below.
    hits = await _npm_search(query, size=limit + 5)
    filtered = [h for h in hits if h.get("name") != package_name][:limit]

    return {
        "package":        package_name,
        "keywords_used":  top_keywords,
        "count":          len(filtered),
        "results":        filtered,
    }


# ---------------------------------------------------------------------------
# TOOL 31: get_packages_by_author
# ---------------------------------------------------------------------------
# npm's search supports an "author:" qualifier. We pass it through verbatim.
@mcp.tool(
    name="get_packages_by_author",
    description=(
        "List all npm packages published by a given author (npm username). "
        "Uses the 'author:' search qualifier."
    ),
)
async def get_packages_by_author(
    username: str = Field(description="npm username, e.g. 'sindresorhus'."),
    limit: int = Field(
        default=50,
        description="Maximum number of packages to return (1–50).",
    ),
) -> dict:
    # Strip a leading @ if the caller was thinking of scoped-package syntax.
    clean = username.lstrip("@").strip()
    hits = await _npm_search(f"author:{clean}", size=limit)
    return {
        "author": clean,
        "count":  len(hits),
        "packages": hits,
    }


# ---------------------------------------------------------------------------
# TOOL 32: get_organization_packages
# ---------------------------------------------------------------------------
# Scoped packages look like @babel/core, @types/node, @vue/reactivity. npm's
# search supports a "scope:" qualifier that accepts either the raw name or
# the @-prefixed form. We accept both from the caller for convenience.
@mcp.tool(
    name="get_organization_packages",
    description=(
        "List all npm packages under a given scope / organization "
        "(e.g. '@babel' → every @babel/* package)."
    ),
)
async def get_organization_packages(
    scope: str = Field(
        description="npm scope/org, e.g. '@babel' or just 'babel'."
    ),
    limit: int = Field(
        default=50,
        description="Maximum number of packages to return (1–50).",
    ),
) -> dict:
    clean = scope.lstrip("@").strip()
    prefix = f"@{clean}/"

    # npm's "scope:" qualifier is unreliable for popular orgs. We over-fetch
    # with the raw "@scope" text query, then strictly filter by prefix —
    # this guarantees correctness for @babel, @types, @vue, etc.
    hits = await _npm_search(f"@{clean}", size=min(limit * 2, 50))
    filtered = [h for h in hits if (h.get("name") or "").startswith(prefix)]
    filtered = filtered[:limit]

    return {
        "scope":    f"@{clean}",
        "count":    len(filtered),
        "packages": filtered,
    }


# ===========================================================================
# UTILITY TOOLS
# ===========================================================================
# Handy helpers that don't fit the other categories:
#   batch_get_versions        - latest-version lookup for N packages at once
#   validate_package_json     - sanity-check dep ranges in a package.json
#   generate_install_command  - build npm / pnpm / yarn install commands
#   resolve_cdn_url           - jsDelivr / unpkg URLs for a pkg[@version][/file]


# ---------------------------------------------------------------------------
# TOOL 33: batch_get_versions
# ---------------------------------------------------------------------------
# Given a list of packages, fetch each one's "latest" in parallel. Much faster
# than calling get_latest_version repeatedly — one HTTP round-trip per package
# instead of a sequential chain.
@mcp.tool(
    name="batch_get_versions",
    description=(
        "Get the latest version for many npm packages at once (parallel "
        "fetch). Returns a {name: version} map plus any per-package errors."
    ),
)
async def batch_get_versions(
    package_names: list[str] = Field(
        description="List of exact npm package names to look up, e.g. ['react', 'lodash', '@types/node']."
    ),
) -> dict:
    if not package_names:
        return {"checked": 0, "versions": {}, "errors": {}}

    async def _one(name: str) -> tuple[str, str | None, str | None]:
        try:
            data = await _fetch_json(
                f"{NPM_REGISTRY}/{name}/latest",
                not_found_msg=f"npm package '{name}' was not found.",
            )
            return name, data.get("version"), None
        except ValueError as exc:
            # Errors are per-package — don't blow up the whole batch.
            return name, None, str(exc)

    # Fan out. npm's CDN handles this fine; we're hitting /latest which is cheap.
    results = await asyncio.gather(*(_one(n) for n in package_names))

    versions: dict[str, str] = {}
    errors:   dict[str, str] = {}
    for name, ver, err in results:
        if err:
            errors[name] = err
        elif ver is not None:
            versions[name] = ver

    return {
        "checked":  len(package_names),
        "resolved": len(versions),
        "failed":   len(errors),
        "versions": versions,
        "errors":   errors,
    }


# ---------------------------------------------------------------------------
# TOOL 34: validate_package_json
# ---------------------------------------------------------------------------
# Sanity-checks the dep ranges a consumer would feed to npm install. For each
# declared range we:
#   1. verify the syntax is a recognisable npm range (leveraging _satisfies)
#   2. resolve it to a concrete version via the same logic as resolve_semver
#   3. flag ranges that don't match ANY published version (likely a typo)
# Accepts either dependency maps directly or a whole package.json object.
@mcp.tool(
    name="validate_package_json",
    description=(
        "Validate the dependency ranges in a package.json. Checks syntax and "
        "verifies each range resolves to at least one published version. "
        "Pass the whole package.json object OR the individual dep maps."
    ),
)
async def validate_package_json(
    package_json: dict = Field(
        default_factory=dict,
        description="Full package.json object. Its dependencies/devDependencies/peerDependencies fields will be validated.",
    ),
    dependencies: dict[str, str] = Field(
        default_factory=dict,
        description="Alternative to package_json — pass a bare {name: range} map directly.",
    ),
) -> dict:
    # Build the unified {name: range} map we'll validate.
    to_check: dict[str, dict[str, str]] = {
        "dependencies":     (package_json.get("dependencies")     or {}),
        "devDependencies":  (package_json.get("devDependencies")  or {}),
        "peerDependencies": (package_json.get("peerDependencies") or {}),
    }
    if dependencies:
        # Merge the bare map under the "dependencies" bucket.
        to_check["dependencies"] = {**to_check["dependencies"], **dependencies}

    # Flat list of (bucket, name, range) for a single parallel fan-out.
    items: list[tuple[str, str, str]] = [
        (bucket, name, range_)
        for bucket, deps in to_check.items()
        for name, range_ in deps.items()
    ]

    async def _validate_one(bucket: str, name: str, range_: str) -> dict:
        # Trivial ranges are always valid and don't need a resolve call.
        if range_ in ("*", "latest") or not range_.strip():
            return {
                "bucket": bucket, "name": name, "range": range_,
                "status": "ok", "resolved": None,
                "note": "Range accepts any version.",
            }

        # Fetch the package, try to find a matching version.
        try:
            pkg = await _fetch_json(
                f"{NPM_REGISTRY}/{name}",
                not_found_msg=f"npm package '{name}' was not found.",
            )
        except ValueError as exc:
            return {
                "bucket": bucket, "name": name, "range": range_,
                "status": "package_not_found", "error": str(exc),
            }

        versions = list((pkg.get("versions", {}) or {}).keys())
        # Reuse the existing matcher from check_peer_compatibility.
        matches = [v for v in versions if _satisfies(v, range_) == "yes"]
        if not matches:
            return {
                "bucket": bucket, "name": name, "range": range_,
                "status": "no_matching_version",
                "note": "Range is syntactically valid but resolves to no published version.",
            }

        def _key(v: str) -> tuple:
            parsed = _parse_semver(v)
            return parsed if parsed is not None else (0, 0, 0)

        return {
            "bucket": bucket, "name": name, "range": range_,
            "status": "ok", "resolved": max(matches, key=_key),
        }

    results = await asyncio.gather(*(_validate_one(*t) for t in items))

    # Summarise by status so callers can see the picture at a glance.
    summary: dict[str, int] = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1

    return {
        "valid":   summary.get("ok", 0) == len(results),
        "checked": len(results),
        "summary": summary,
        "results": results,
    }


# ---------------------------------------------------------------------------
# TOOL 35: generate_install_command
# ---------------------------------------------------------------------------
# Turns a list of packages into shell install commands for the three main
# package managers. Covers the common flags: --save-dev, --save-exact. No
# network calls — this is pure string assembly.
@mcp.tool(
    name="generate_install_command",
    description=(
        "Generate install commands for npm / pnpm / yarn / bun from a list "
        "of packages (each optionally pinned to a version). Supports dev "
        "and exact flags."
    ),
)
async def generate_install_command(
    packages: list[str] = Field(
        description=(
            "List of package names. Each entry can include a version: "
            "'react', 'react@18.2.0', '@babel/core@^7'."
        )
    ),
    dev: bool = Field(
        default=False,
        description="If True, install as a devDependency.",
    ),
    exact: bool = Field(
        default=False,
        description="If True, pin versions exactly (no caret/tilde).",
    ),
) -> dict:
    if not packages:
        raise ValueError("At least one package name is required.")

    # Build the unified package-arg string — same across managers.
    args = " ".join(packages)

    # Assemble a command per manager with the right flag dialect.
    npm_flags = []
    if dev:   npm_flags.append("--save-dev")
    if exact: npm_flags.append("--save-exact")
    npm_cmd  = "npm install " + (" ".join(npm_flags) + " " if npm_flags else "") + args

    pnpm_flags = []
    if dev:   pnpm_flags.append("--save-dev")
    if exact: pnpm_flags.append("--save-exact")
    pnpm_cmd = "pnpm add " + (" ".join(pnpm_flags) + " " if pnpm_flags else "") + args

    yarn_flags = []
    if dev:   yarn_flags.append("--dev")
    if exact: yarn_flags.append("--exact")
    yarn_cmd = "yarn add " + (" ".join(yarn_flags) + " " if yarn_flags else "") + args

    bun_flags = []
    if dev:   bun_flags.append("--dev")
    if exact: bun_flags.append("--exact")
    bun_cmd = "bun add " + (" ".join(bun_flags) + " " if bun_flags else "") + args

    return {
        "packages": packages,
        "dev":      dev,
        "exact":    exact,
        "commands": {
            "npm":  npm_cmd,
            "pnpm": pnpm_cmd,
            "yarn": yarn_cmd,
            "bun":  bun_cmd,
        },
    }


# ---------------------------------------------------------------------------
# TOOL 36: resolve_cdn_url
# ---------------------------------------------------------------------------
# Two popular npm-backed CDNs: jsDelivr and unpkg. Both accept the same URL
# shape: https://<cdn>/<pkg>[@<version>][/<file>]. We emit both. If no
# version is given, we fetch the latest so the returned URLs are pinned.
@mcp.tool(
    name="resolve_cdn_url",
    description=(
        "Build jsDelivr + unpkg CDN URLs for an npm package. Version-pins "
        "to latest when no version is provided. Optional file path within "
        "the package (e.g. 'dist/index.min.js')."
    ),
)
async def resolve_cdn_url(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        default="",
        description="Exact version. Leave empty to pin to latest.",
    ),
    file: str = Field(
        default="",
        description=(
            "Optional path within the package (e.g. 'dist/react.production.min.js'). "
            "Leave empty for the package root."
        ),
    ),
) -> dict:
    # Resolve "latest" so the URLs we hand back are actually immutable.
    resolved_version = version
    if not resolved_version:
        data = await _fetch_json(
            f"{NPM_REGISTRY}/{package_name}/latest",
            not_found_msg=f"npm package '{package_name}' was not found.",
        )
        resolved_version = data.get("version", "")
        if not resolved_version:
            raise ValueError(
                f"Could not resolve latest version for '{package_name}'."
            )

    # Normalise the file path — strip leading "/" so we don't double up.
    file_suffix = ""
    if file:
        file_suffix = "/" + file.lstrip("/")

    base = f"{package_name}@{resolved_version}{file_suffix}"

    return {
        "package":  package_name,
        "version":  resolved_version,
        "file":     file or None,
        "jsdelivr": f"https://cdn.jsdelivr.net/npm/{base}",
        "unpkg":    f"https://unpkg.com/{base}",
        # ESM variant is handy for modern <script type="module"> usage.
        "esm_sh":   f"https://esm.sh/{base}",
    }


# ===========================================================================
# DOWNLOAD DEEP-DIVE TOOLS (extends Security & Health)
# ===========================================================================
# Complements get_download_stats with three richer views:
#   get_download_trend        - day-by-day counts over a range
#   compare_popularity        - side-by-side weekly counts for 2-5 packages
#   get_download_by_version   - which versions are people actually installing?


# ---------------------------------------------------------------------------
# TOOL 37: get_download_trend
# ---------------------------------------------------------------------------
# api.npmjs.org exposes a "range" endpoint that returns daily counts for up
# to 18 months. We derive a simple trend slope (growing / declining / flat)
# by comparing the first and last quartiles of the series — good enough to
# answer "is this package growing or dying?" without pulling in a chart lib.
@mcp.tool(
    name="get_download_trend",
    description=(
        "Get day-by-day download counts over a range (e.g. 'last-month', "
        "'last-year', or a 'YYYY-MM-DD:YYYY-MM-DD' pair). Includes a simple "
        "growing / declining / flat trend label."
    ),
)
async def get_download_trend(
    package_name: str = Field(description="Exact npm package name."),
    period: str = Field(
        default="last-month",
        description=(
            "One of: 'last-day', 'last-week', 'last-month', 'last-year', "
            "or a custom 'YYYY-MM-DD:YYYY-MM-DD' range (max 540 days)."
        ),
    ),
) -> dict:
    url = f"https://api.npmjs.org/downloads/range/{period}/{package_name}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(url)
    except httpx.RequestError as exc:
        raise ValueError(f"Could not reach npm download API: {exc}") from exc

    if response.status_code == 404:
        raise ValueError(f"npm package '{package_name}' was not found.")
    if response.status_code >= 400:
        raise ValueError(
            f"npm downloads API returned HTTP {response.status_code}."
        )

    data = response.json()
    series = data.get("downloads", []) or []
    # Each entry is {"day": "YYYY-MM-DD", "downloads": int}.
    counts = [d.get("downloads", 0) for d in series]
    total = sum(counts)

    # Compare the first and last quartiles for a robust trend signal — a
    # single-day spike won't flip the result the way a raw first-vs-last
    # comparison would.
    trend = "unknown"
    first_q_avg = 0.0
    last_q_avg = 0.0
    if len(counts) >= 4:
        q = max(1, len(counts) // 4)
        first_q_avg = sum(counts[:q]) / q
        last_q_avg  = sum(counts[-q:]) / q
        if first_q_avg == 0 and last_q_avg == 0:
            trend = "zero"
        elif first_q_avg == 0:
            trend = "growing"
        else:
            ratio = last_q_avg / first_q_avg
            if ratio >= 1.2:
                trend = "growing"
            elif ratio <= 0.8:
                trend = "declining"
            else:
                trend = "flat"

    return {
        "package":        package_name,
        "period":         period,
        "start":          data.get("start"),
        "end":            data.get("end"),
        "total":          total,
        "days":           len(series),
        "average_daily":  round(total / len(series), 1) if series else 0,
        "peak_day":       max(series, key=lambda d: d.get("downloads", 0))
                          if series else None,
        "trend":          trend,
        "first_quarter_avg": round(first_q_avg, 1),
        "last_quarter_avg":  round(last_q_avg, 1),
        "series":         series,   # full daily series for charting
    }


# ---------------------------------------------------------------------------
# TOOL 38: compare_popularity
# ---------------------------------------------------------------------------
# The download API accepts comma-separated package names (up to 128) at the
# /point endpoint. We use that for scoped calls that fit in one request, plus
# a parallel fallback for odd edge cases. Ranks the results and returns a
# winner.
@mcp.tool(
    name="compare_popularity",
    description=(
        "Compare weekly or monthly download counts for 2–10 npm packages "
        "side by side. Returns sorted ranking, absolute numbers, and each "
        "package's share of the total."
    ),
)
async def compare_popularity(
    packages: list[str] = Field(
        description="List of 2–10 exact npm package names to compare."
    ),
    period: str = Field(
        default="last-week",
        description="One of: 'last-day', 'last-week', 'last-month'.",
    ),
) -> dict:
    if not packages or len(packages) < 2:
        raise ValueError("Provide at least 2 package names to compare.")
    if len(packages) > 10:
        raise ValueError("compare_popularity supports at most 10 packages per call.")

    # Scoped packages (starting with "@") can't be mixed into the bulk
    # comma-separated form — npm's endpoint rejects that. Split into a bulk
    # batch + per-package batch for scoped names.
    bulk = [p for p in packages if not p.startswith("@")]
    scoped = [p for p in packages if p.startswith("@")]

    downloads: dict[str, int] = {}

    async def _fetch_single(name: str) -> tuple[str, int]:
        url = f"https://api.npmjs.org/downloads/point/{period}/{name}"
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                r = await client.get(url)
            if r.status_code != 200:
                return name, 0
            return name, r.json().get("downloads", 0) or 0
        except httpx.RequestError:
            return name, 0

    if bulk:
        # Bulk endpoint: one HTTP call for a list of non-scoped packages.
        # IMPORTANT: npm returns TWO different shapes here:
        #   - single package:    {downloads, start, end, package}
        #   - multiple packages: {pkg1: {...}, pkg2: {...}}
        # We always hit the single-package path when bulk has just one name.
        if len(bulk) == 1:
            pairs = await asyncio.gather(*(_fetch_single(n) for n in bulk))
            downloads.update(dict(pairs))
        else:
            url = f"https://api.npmjs.org/downloads/point/{period}/{','.join(bulk)}"
            try:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                    r = await client.get(url)
                if r.status_code == 200:
                    data = r.json()
                    for name in bulk:
                        entry = data.get(name) or {}
                        downloads[name] = entry.get("downloads", 0) or 0
                else:
                    # Fall back to per-package fetch if bulk fails.
                    pairs = await asyncio.gather(*(_fetch_single(n) for n in bulk))
                    downloads.update(dict(pairs))
            except httpx.RequestError as exc:
                raise ValueError(f"Could not reach npm download API: {exc}") from exc

    if scoped:
        # Scoped packages — parallel fan-out.
        pairs = await asyncio.gather(*(_fetch_single(n) for n in scoped))
        downloads.update(dict(pairs))

    # Rank highest-first and compute each package's share of the total.
    total = sum(downloads.values())
    ranked = sorted(downloads.items(), key=lambda kv: kv[1], reverse=True)
    ranking = [
        {
            "rank":     i,
            "name":     name,
            "downloads": count,
            "share":    round((count / total) * 100, 2) if total else 0.0,
        }
        for i, (name, count) in enumerate(ranked, 1)
    ]

    return {
        "period":  period,
        "total":   total,
        "winner":  ranked[0][0] if ranked else None,
        "ranking": ranking,
    }


# ---------------------------------------------------------------------------
# TOOL 39: get_download_by_version
# ---------------------------------------------------------------------------
# api.npmjs.org exposes a per-version breakdown: which versions are people
# actually installing? This is the "are users still on v16 even though v18
# is out?" question — crucial context for maintainers planning deprecations.
@mcp.tool(
    name="get_download_by_version",
    description=(
        "Get last-week download counts broken down by version. Answers "
        "'which version is actually being used?' — useful for maintainers "
        "planning deprecations and for consumers gauging real-world adoption."
    ),
)
async def get_download_by_version(
    package_name: str = Field(description="Exact npm package name."),
    top_n: int = Field(
        default=10,
        description="Return only the top N versions by download count (1–50).",
    ),
) -> dict:
    url = f"https://api.npmjs.org/versions/{package_name}/last-week"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(url)
    except httpx.RequestError as exc:
        raise ValueError(f"Could not reach npm versions API: {exc}") from exc

    if response.status_code == 404:
        raise ValueError(f"npm package '{package_name}' was not found.")
    if response.status_code >= 400:
        raise ValueError(
            f"npm versions API returned HTTP {response.status_code}."
        )

    data = response.json()
    versions = data.get("downloads", {}) or {}
    total = sum(versions.values())

    # Sort by download count, descending. The raw response is unordered.
    sorted_items = sorted(versions.items(), key=lambda kv: kv[1], reverse=True)
    cap = max(1, min(int(top_n), 50))
    top = sorted_items[:cap]

    # Identify the most-downloaded major version family (e.g. v17 vs v18).
    major_totals: dict[str, int] = {}
    for ver, count in sorted_items:
        parsed = _parse_semver(ver)
        if parsed is None:
            continue
        key = f"{parsed[0]}.x"
        major_totals[key] = major_totals.get(key, 0) + count
    top_major = max(major_totals.items(), key=lambda kv: kv[1]) if major_totals else None

    return {
        "package":      package_name,
        "period":       "last-week",
        "total":        total,
        "total_versions": len(versions),
        "top_versions": [
            {
                "version":   ver,
                "downloads": count,
                "share":     round((count / total) * 100, 2) if total else 0.0,
            }
            for ver, count in top
        ],
        "most_popular_major": (
            {"major": top_major[0], "downloads": top_major[1]}
            if top_major else None
        ),
    }


# ===========================================================================
# BUNDLE SIZE TOOLS
# ===========================================================================
# Uses bundlephobia.com's free public API to answer "how heavy is this on
# the client?" questions. bundlephobia's numbers are the de-facto standard
# for client-side JS bundle reasoning — every major front-end tool references
# them (create-react-app, nextjs, rollup, etc.).
#
#   get_bundle_size           - size + gzip for a pkg[@version]
#   get_bundle_size_history   - size across many versions
#   check_treeshakeable       - does the pkg ship ES modules + no side-effects?
#   compare_bundle_sizes      - side-by-side size ranking
#   get_bundle_size_impact    - framed as "what does this cost my bundle?"

BUNDLEPHOBIA = "https://bundlephobia.com/api"
# bundlephobia can be slow — give it its own longer timeout. The site builds
# the package on-demand if it hasn't been seen before, which takes 5-10s.
BUNDLEPHOBIA_TIMEOUT = 20.0


async def _fetch_bundle_size(package_name: str, version: str = "") -> dict:
    """Shared helper: fetch bundle size from bundlephobia."""
    pkg = f"{package_name}@{version}" if version else package_name
    url = f"{BUNDLEPHOBIA}/size?package={pkg}"
    try:
        async with httpx.AsyncClient(timeout=BUNDLEPHOBIA_TIMEOUT) as client:
            response = await client.get(url)
    except httpx.RequestError as exc:
        raise ValueError(f"Could not reach bundlephobia: {exc}") from exc

    if response.status_code == 404:
        raise ValueError(
            f"bundlephobia has no data for '{pkg}' (unpublished or too new?)."
        )
    if response.status_code >= 400:
        # bundlephobia returns a JSON error body when it can't build the pkg.
        try:
            err = response.json().get("error", {}).get("message", "")
        except Exception:
            err = ""
        raise ValueError(
            f"bundlephobia returned HTTP {response.status_code} for '{pkg}'"
            + (f": {err}" if err else ".")
        )

    return response.json()


def _format_bytes(n: int | None) -> str:
    """Turn raw bytes into something a human can skim."""
    if n is None:
        return "unknown"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f} MB"
    if n >= 1_000:
        return f"{n / 1_000:.2f} KB"
    return f"{n} B"


# ---------------------------------------------------------------------------
# TOOL 40: get_bundle_size
# ---------------------------------------------------------------------------
# The bread-and-butter size check. Returns raw size, gzipped size, and the
# direct dependency count — what every front-end reviewer wants to see
# before approving a new import.
@mcp.tool(
    name="get_bundle_size",
    description=(
        "Get the minified and gzipped bundle size of an npm package via "
        "bundlephobia. Returns human-readable sizes plus dependency count."
    ),
)
async def get_bundle_size(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        default="",
        description="Exact version. Leave empty for the latest published version.",
    ),
) -> dict:
    data = await _fetch_bundle_size(package_name, version)
    return {
        "package":         data.get("name"),
        "version":         data.get("version"),
        "size_bytes":      data.get("size"),
        "size_human":      _format_bytes(data.get("size")),
        "gzip_bytes":      data.get("gzip"),
        "gzip_human":      _format_bytes(data.get("gzip")),
        "dependency_count": data.get("dependencyCount"),
        "is_scoped":       data.get("scoped", False),
        "has_side_effects": data.get("hasSideEffects", True),
        "has_js_module":   bool(data.get("hasJSModule")),
        "description":     data.get("description", ""),
        "repository":      data.get("repository", ""),
    }


# ---------------------------------------------------------------------------
# TOOL 41: get_bundle_size_history
# ---------------------------------------------------------------------------
# bundlephobia's /package-history returns size for many versions in one call.
# Useful for "has this package gotten heavier over time?" — a real concern
# for packages like lodash, moment, three.js.
@mcp.tool(
    name="get_bundle_size_history",
    description=(
        "Get the bundle-size history across many versions of an npm package. "
        "Reports whether the package has grown, shrunk, or stayed flat."
    ),
)
async def get_bundle_size_history(
    package_name: str = Field(description="Exact npm package name."),
    limit: int = Field(
        default=10,
        description="Max number of historical versions to report (1–30).",
    ),
) -> dict:
    safe_limit = max(1, min(int(limit), 30))
    url = f"{BUNDLEPHOBIA}/package-history?package={package_name}&record-count={safe_limit}"
    try:
        async with httpx.AsyncClient(timeout=BUNDLEPHOBIA_TIMEOUT) as client:
            response = await client.get(url)
    except httpx.RequestError as exc:
        raise ValueError(f"Could not reach bundlephobia: {exc}") from exc

    if response.status_code == 404:
        raise ValueError(f"bundlephobia has no data for '{package_name}'.")
    if response.status_code >= 400:
        raise ValueError(
            f"bundlephobia returned HTTP {response.status_code} for '{package_name}'."
        )

    data = response.json()
    # Response is a map of version -> {size, gzip, version, ...}.
    entries = []
    for version, info in data.items():
        if not isinstance(info, dict):
            continue
        size = info.get("size")
        gzip = info.get("gzip")
        entries.append({
            "version":    version,
            "size_bytes": size,
            "size_human": _format_bytes(size),
            "gzip_bytes": gzip,
            "gzip_human": _format_bytes(gzip),
        })

    # Sort by semver — newest first so "history" reads top-to-bottom = new-to-old.
    def _key(e: dict) -> tuple:
        parsed = _parse_semver(e["version"])
        return parsed if parsed is not None else (0, 0, 0)
    entries.sort(key=_key, reverse=True)

    # bundlephobia often returns stale entries with no size — drop those.
    valid_entries = [e for e in entries if e.get("size_bytes") is not None]
    # Honour the caller's limit on entries with real data.
    trimmed = valid_entries[:safe_limit]

    # Compute trend: compare the oldest vs newest version size in the window.
    trend = "unknown"
    delta_pct = None
    if len(trimmed) >= 2:
        old_size = trimmed[-1]["size_bytes"]
        new_size = trimmed[0]["size_bytes"]
        if old_size and new_size:
            delta_pct = round(((new_size - old_size) / old_size) * 100, 1)
            if delta_pct > 15:
                trend = "growing"
            elif delta_pct < -15:
                trend = "shrinking"
            else:
                trend = "stable"

    return {
        "package":       package_name,
        "count":         len(trimmed),
        "total_returned_by_api": len(entries),
        "skipped_unmeasured":    len(entries) - len(valid_entries),
        "trend":         trend,
        "delta_percent": delta_pct,
        "history":       trimmed,
    }


# ---------------------------------------------------------------------------
# TOOL 42: check_treeshakeable
# ---------------------------------------------------------------------------
# A package is "tree-shakeable" when bundlers can drop unused exports. Two
# conditions in package.json make this possible:
#   1. "module" field (or "exports.import") points to an ESM build  (hasJSModule)
#   2. "sideEffects": false                                         (hasSideEffects)
# Both are needed — CJS-only packages can't be tree-shaken, and packages
# with side effects prevent bundlers from dropping their code.
@mcp.tool(
    name="check_treeshakeable",
    description=(
        "Check if an npm package is tree-shakeable (ships ES modules AND "
        "declares no side-effects). Returns a verdict plus the reasoning."
    ),
)
async def check_treeshakeable(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        default="",
        description="Exact version. Leave empty for the latest version.",
    ),
) -> dict:
    data = await _fetch_bundle_size(package_name, version)

    has_esm        = bool(data.get("hasJSModule"))
    has_side_eff   = data.get("hasSideEffects", True)
    # hasSideEffects can be True, False, or a list/regex of file globs.
    # We treat anything that isn't explicit-False as "yes, there are side-effects".
    declares_none  = has_side_eff is False

    treeshakeable = has_esm and declares_none

    if treeshakeable:
        reason = "Ships ES modules AND declares no side-effects."
    elif not has_esm and not declares_none:
        reason = "No ES module build and side-effects not declared as false."
    elif not has_esm:
        reason = "No ES module build — bundlers cannot drop unused exports."
    else:
        reason = "ES module build present, but side-effects are not declared as false."

    return {
        "package":      data.get("name"),
        "version":      data.get("version"),
        "treeshakeable": treeshakeable,
        "has_esm":      has_esm,
        "side_effects_declared_none": declares_none,
        "reason":       reason,
    }


# ---------------------------------------------------------------------------
# TOOL 43: compare_bundle_sizes
# ---------------------------------------------------------------------------
# Parallel bundlephobia lookups for a set of packages, then rank by gzipped
# size. Classic use case: "axios vs fetch vs ky — which is lightest?"
@mcp.tool(
    name="compare_bundle_sizes",
    description=(
        "Compare the bundle sizes of 2–10 npm packages side by side. "
        "Ranks by gzipped size (ascending) so the lightest wins."
    ),
)
async def compare_bundle_sizes(
    packages: list[str] = Field(
        description=(
            "List of 2–10 packages. Each entry can include a version: "
            "'react', 'react@18.2.0', '@babel/core@^7'."
        )
    ),
) -> dict:
    if not packages or len(packages) < 2:
        raise ValueError("Provide at least 2 packages to compare.")
    if len(packages) > 10:
        raise ValueError("compare_bundle_sizes supports at most 10 packages.")

    async def _one(spec: str) -> dict:
        # Split "name@version" — but preserve @scope/name.
        at_idx = spec.rfind("@")
        if at_idx > 0:
            name, version = spec[:at_idx], spec[at_idx + 1:]
        else:
            name, version = spec, ""
        try:
            data = await _fetch_bundle_size(name, version)
            return {
                "name":       data.get("name", name),
                "version":    data.get("version"),
                "size_bytes": data.get("size"),
                "size_human": _format_bytes(data.get("size")),
                "gzip_bytes": data.get("gzip"),
                "gzip_human": _format_bytes(data.get("gzip")),
                "has_esm":    bool(data.get("hasJSModule")),
            }
        except ValueError as exc:
            return {
                "name": name,
                "version": version,
                "error": str(exc),
            }

    results = await asyncio.gather(*(_one(p) for p in packages))

    # Rank on gzip (what actually reaches the user). Missing sizes go last.
    def _rank_key(r: dict) -> tuple[int, int]:
        has_data = 0 if "error" in r or r.get("gzip_bytes") is None else 1
        return (-has_data, r.get("gzip_bytes") or 10**12)

    ranked = sorted(results, key=_rank_key)
    lightest = next((r for r in ranked if "error" not in r and r.get("gzip_bytes")), None)

    return {
        "count":    len(results),
        "lightest": lightest.get("name") if lightest else None,
        "ranking":  [
            {
                "rank": i,
                **r,
            }
            for i, r in enumerate(ranked, 1)
        ],
    }


# ---------------------------------------------------------------------------
# TOOL 44: get_bundle_size_impact
# ---------------------------------------------------------------------------
# Same data as get_bundle_size but framed for the "should I add this dep?"
# question. Calls out the transitive dependency count and gzipped weight
# in a single sentence the reviewer can paste into a PR comment.
@mcp.tool(
    name="get_bundle_size_impact",
    description=(
        "Estimate the impact of adding an npm package to your bundle. "
        "Returns sizes, transitive dep count, and a one-line reviewer summary."
    ),
)
async def get_bundle_size_impact(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        default="",
        description="Exact version. Leave empty for the latest version.",
    ),
) -> dict:
    data = await _fetch_bundle_size(package_name, version)

    size = data.get("size") or 0
    gzip = data.get("gzip") or 0
    dep_count = data.get("dependencyCount", 0)

    # Impact tier based on gzipped size — mirrors what most reviewers
    # intuitively worry about (a 50 KB gzip dep is a lot; a 1 KB dep is noise).
    if gzip >= 100_000:
        impact = "heavy"
    elif gzip >= 30_000:
        impact = "moderate"
    elif gzip >= 5_000:
        impact = "small"
    else:
        impact = "tiny"

    # One-line summary ready for a PR comment.
    summary = (
        f"Adding {data.get('name')}@{data.get('version')} will add "
        f"~{_format_bytes(gzip)} gzipped "
        f"({_format_bytes(size)} minified) to your bundle, pulling in "
        f"{dep_count} transitive dependencies. Impact: {impact}."
    )

    return {
        "package":         data.get("name"),
        "version":         data.get("version"),
        "size_bytes":      size,
        "size_human":      _format_bytes(size),
        "gzip_bytes":      gzip,
        "gzip_human":      _format_bytes(gzip),
        "dependency_count": dep_count,
        "impact":          impact,
        "summary":         summary,
    }


# ===========================================================================
# ADVANCED SECURITY TOOLS (extends Security & Health)
# ===========================================================================
# Deeper OSV-backed tools for vulnerability research:
#   get_vulnerability_details - look up a specific CVE/GHSA by ID
#   audit_all_dependencies    - scan a whole package.json for vulns
#   check_supply_chain_risk   - flag vulnerable direct + transitive deps
#   get_patched_version       - which version fixed a given advisory?


OSV_API = "https://api.osv.dev/v1"


# ---------------------------------------------------------------------------
# TOOL 45: get_vulnerability_details
# ---------------------------------------------------------------------------
# Given a CVE / GHSA / OSV ID, fetch the full advisory record. We trim the
# response so callers get the useful fields (summary, severity, affected
# versions, patched versions) without parsing the enormous raw OSV schema.
@mcp.tool(
    name="get_vulnerability_details",
    description=(
        "Fetch full details for a specific vulnerability by ID (e.g. "
        "'GHSA-29mw-wpgm-hmr9', 'CVE-2024-1234'). Returns summary, severity, "
        "affected versions, and patched versions."
    ),
)
async def get_vulnerability_details(
    vuln_id: str = Field(
        description="The advisory ID — GHSA, CVE, OSV, RUSTSEC, etc."
    ),
) -> dict:
    url = f"{OSV_API}/vulns/{vuln_id}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(url)
    except httpx.RequestError as exc:
        raise ValueError(f"Could not reach OSV.dev: {exc}") from exc

    if response.status_code == 404:
        raise ValueError(f"Vulnerability '{vuln_id}' not found in OSV.")
    if response.status_code >= 400:
        raise ValueError(
            f"OSV returned HTTP {response.status_code} for '{vuln_id}'."
        )

    data = response.json()

    # Flatten "affected" into a simpler per-package summary.
    affected_summary = []
    for aff in data.get("affected", []) or []:
        pkg = aff.get("package", {}) or {}
        if pkg.get("ecosystem") != "npm":
            continue
        ranges_info = []
        fixed_versions = []
        for r in aff.get("ranges", []) or []:
            events = r.get("events", []) or []
            introduced = next(
                (e.get("introduced") for e in events if "introduced" in e),
                None,
            )
            fixed = next(
                (e.get("fixed") for e in events if "fixed" in e),
                None,
            )
            ranges_info.append({"introduced": introduced, "fixed": fixed})
            if fixed:
                fixed_versions.append(fixed)
        affected_summary.append({
            "package": pkg.get("name"),
            "ranges":  ranges_info,
            "fixed_versions": fixed_versions,
        })

    severity = data.get("severity", []) or []
    severity_summary = [
        {"type": s.get("type"), "score": s.get("score")}
        for s in severity
    ]

    return {
        "id":            data.get("id"),
        "aliases":       data.get("aliases", []),
        "summary":       data.get("summary", ""),
        "details":       data.get("details", "")[:2000],   # can be long
        "published":     data.get("published"),
        "modified":      data.get("modified"),
        "severity":      severity_summary,
        "affected_npm":  affected_summary,
        "references":   [r.get("url") for r in data.get("references", []) or []][:10],
    }


# ---------------------------------------------------------------------------
# TOOL 46: audit_all_dependencies
# ---------------------------------------------------------------------------
# Supply-chain scan of a whole package.json. We use OSV's /querybatch endpoint
# — one HTTP call that reports vulns across N packages at once. Much faster
# than a per-package loop for a typical lock file with dozens of deps.
@mcp.tool(
    name="audit_all_dependencies",
    description=(
        "Audit a package.json (or a {name: version} map) for known "
        "vulnerabilities. Uses OSV's batch endpoint — one HTTP call for "
        "the whole dependency set."
    ),
)
async def audit_all_dependencies(
    package_json: dict = Field(
        default_factory=dict,
        description="Full package.json object. Its dependencies/devDependencies/peerDependencies will be audited.",
    ),
    dependencies: dict[str, str] = Field(
        default_factory=dict,
        description="Alternative: a bare {name: version} map to audit directly.",
    ),
    include_dev: bool = Field(
        default=False,
        description="If True, also include devDependencies in the audit.",
    ),
) -> dict:
    # Build the merged dependency map to audit.
    to_audit: dict[str, str] = {}
    to_audit.update(package_json.get("dependencies", {}) or {})
    to_audit.update(package_json.get("peerDependencies", {}) or {})
    if include_dev:
        to_audit.update(package_json.get("devDependencies", {}) or {})
    if dependencies:
        to_audit.update(dependencies)

    if not to_audit:
        return {
            "checked": 0,
            "vulnerable_count": 0,
            "packages": [],
            "note": "No dependencies provided to audit.",
        }

    # Resolve each range to a concrete version first. OSV needs a specific
    # version to check, not a range. We reuse resolve_semver's logic inline.
    async def _resolve(name: str, range_: str) -> str | None:
        # Simple ranges: strip common prefix and treat the rest as a version.
        stripped = range_.lstrip("^~>=< ").split(" ")[0].strip()
        if not stripped or stripped in ("*", "latest"):
            # Fetch latest when range is wildcard-ish.
            try:
                data = await _fetch_json(
                    f"{NPM_REGISTRY}/{name}/latest",
                    not_found_msg=f"'{name}' not found.",
                )
                return data.get("version")
            except ValueError:
                return None
        # If the "version" is itself a valid version, use it directly.
        if _parse_semver(stripped) is not None:
            return stripped
        return None

    # Resolve everything in parallel.
    names = list(to_audit.keys())
    ranges = [to_audit[n] for n in names]
    resolved = await asyncio.gather(
        *(_resolve(n, r) for n, r in zip(names, ranges))
    )

    # Build the OSV batch query — one entry per (name, version) pair that
    # resolved successfully. Unresolved packages are reported as errors.
    queries = []
    query_to_name: list[str] = []
    unresolved: list[dict] = []
    for name, range_, ver in zip(names, ranges, resolved):
        if ver is None:
            unresolved.append({
                "package": name, "range": range_,
                "error": "Could not resolve range to a concrete version.",
            })
            continue
        queries.append({
            "package": {"name": name, "ecosystem": "npm"},
            "version": ver,
        })
        query_to_name.append(name)

    vulnerable: list[dict] = []
    if queries:
        # OSV's batch endpoint: POST /v1/querybatch with {"queries": [...]}.
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT * 2) as client:
                response = await client.post(
                    f"{OSV_API}/querybatch",
                    json={"queries": queries},
                )
        except httpx.RequestError as exc:
            raise ValueError(f"Could not reach OSV.dev: {exc}") from exc

        if response.status_code >= 400:
            raise ValueError(
                f"OSV batch API returned HTTP {response.status_code}."
            )

        results = response.json().get("results", []) or []
        for name, q, r in zip(query_to_name, queries, results):
            vulns = r.get("vulns", []) or []
            if not vulns:
                continue
            vulnerable.append({
                "package":    name,
                "version":    q["version"],
                "vuln_count": len(vulns),
                "vuln_ids":   [v.get("id") for v in vulns][:10],
            })

    return {
        "checked":          len(queries),
        "vulnerable_count": len(vulnerable),
        "safe_count":       len(queries) - len(vulnerable),
        "unresolved_count": len(unresolved),
        "packages":         vulnerable,
        "unresolved":       unresolved,
    }


# ---------------------------------------------------------------------------
# TOOL 47: check_supply_chain_risk
# ---------------------------------------------------------------------------
# "If I install X, what vulnerable code am I pulling in?" We walk one level
# of direct dependencies (depth=1) and audit them all. A deeper walk is
# possible but explodes quickly — keep it shallow by default.
@mcp.tool(
    name="check_supply_chain_risk",
    description=(
        "Check if any direct dependencies of a given npm package have known "
        "vulnerabilities. Reports a risk level and the full per-dep audit."
    ),
)
async def check_supply_chain_risk(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        default="",
        description="Exact version. Leave empty for the latest version.",
    ),
) -> dict:
    manifest = await _fetch_manifest(package_name, version)
    deps = manifest.get("dependencies", {}) or {}

    # Re-use audit_all_dependencies' logic.
    audit = await audit_all_dependencies(
        package_json={},
        dependencies=deps,
        include_dev=False,
    )

    vuln_count = audit.get("vulnerable_count", 0)
    checked = audit.get("checked", 0)

    # Risk tier — simple heuristic.
    if vuln_count == 0:
        risk = "clean"
    elif vuln_count <= 1 and checked >= 5:
        risk = "low"
    elif vuln_count <= 3:
        risk = "medium"
    else:
        risk = "high"

    return {
        "package":           manifest.get("name", package_name),
        "version":           manifest.get("version"),
        "direct_dep_count":  checked,
        "vulnerable_deps":   vuln_count,
        "risk":              risk,
        "details":           audit.get("packages", []),
        "unresolved":        audit.get("unresolved", []),
    }


# ---------------------------------------------------------------------------
# TOOL 48: get_patched_version
# ---------------------------------------------------------------------------
# Given an advisory ID, look at its "affected" ranges and extract the first
# "fixed" version for each affected npm package. Answers "what version do
# I need to upgrade to in order to be safe?".
@mcp.tool(
    name="get_patched_version",
    description=(
        "Given a vulnerability ID, return the patched (fixed) versions per "
        "affected npm package — the versions you need to upgrade to."
    ),
)
async def get_patched_version(
    vuln_id: str = Field(description="Advisory ID (GHSA, CVE, OSV, ...).")
) -> dict:
    details = await get_vulnerability_details(vuln_id=vuln_id)

    patches = []
    for aff in details.get("affected_npm", []) or []:
        fixes = aff.get("fixed_versions") or []
        if not fixes:
            patches.append({
                "package": aff.get("package"),
                "fix_available": False,
                "note": "No fix version declared in the advisory.",
            })
            continue
        # Pick the lowest fix version — usually the minimum safe upgrade.
        def _key(v: str) -> tuple:
            parsed = _parse_semver(v)
            return parsed if parsed is not None else (0, 0, 0)

        first_fix = min(fixes, key=_key)
        patches.append({
            "package":        aff.get("package"),
            "fix_available":  True,
            "first_patched":  first_fix,
            "all_patched":    fixes,
            "affected_ranges": aff.get("ranges", []),
        })

    return {
        "id":       details.get("id"),
        "summary":  details.get("summary", ""),
        "patches":  patches,
    }


# ===========================================================================
# MODULE & COMPATIBILITY TOOLS
# ===========================================================================
# Answers "what module systems / runtimes does this package support?":
#   check_esm_support        - ES modules (import X from 'pkg')
#   check_cjs_support        - CommonJS (require('pkg'))
#   check_typescript_support - has built-in .d.ts or a DefinitelyTyped package
#   get_exports_map          - the "exports" field from package.json
#   check_browser_compatible - safe to bundle for the browser?
#   check_deno_compatible    - runs under Deno?
#   get_package_on_jsr       - also published on the JSR registry?


def _manifest_has_esm(manifest: dict) -> tuple[bool, list[str]]:
    """Return (has_esm, reasons). Checks the many signals npm uses for ESM."""
    reasons = []
    has = False
    # Modern: "exports" with an "import" condition (or "module" condition).
    exp = manifest.get("exports")
    if isinstance(exp, dict):
        def _walk(obj):
            nonlocal has
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in ("import", "module", "default"):
                        if isinstance(v, str) and v:
                            has = True
                    _walk(v)
        _walk(exp)
        if has:
            reasons.append("exports field declares an 'import' condition")
    # Legacy but still common: top-level "module" field → ESM entry point.
    if manifest.get("module"):
        has = True
        reasons.append('has a "module" field pointing to an ESM build')
    # "type": "module" treats all .js files as ESM.
    if manifest.get("type") == "module":
        has = True
        reasons.append('"type": "module" in package.json')
    return has, reasons


def _manifest_has_cjs(manifest: dict) -> tuple[bool, list[str]]:
    """Return (has_cjs, reasons) for CommonJS support."""
    reasons = []
    has = False
    exp = manifest.get("exports")
    if isinstance(exp, dict):
        def _walk(obj):
            nonlocal has
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in ("require", "default"):
                        if isinstance(v, str) and v:
                            has = True
                    _walk(v)
        _walk(exp)
        if has:
            reasons.append("exports field declares a 'require' condition")
    # "main" is the classic CJS entry point.
    if manifest.get("main"):
        # Unless "type": "module" and main has .js — then main is ESM too.
        if manifest.get("type") != "module":
            has = True
            reasons.append('has a "main" field (classic CommonJS entry)')
    # If there's no exports map, no type=module, and no module field but there
    # ARE files, npm treats them as CJS by default.
    if not exp and not manifest.get("module") and manifest.get("type") != "module":
        if manifest.get("main") or manifest.get("files"):
            if not has:
                has = True
                reasons.append("no ESM markers — defaults to CommonJS")
    return has, reasons


# ---------------------------------------------------------------------------
# TOOL 49: check_esm_support
# ---------------------------------------------------------------------------
@mcp.tool(
    name="check_esm_support",
    description=(
        "Check whether an npm package supports ES Modules. Looks at exports "
        "conditions, the 'module' field, and 'type: module' — all the modern "
        "signals npm uses to declare ESM."
    ),
)
async def check_esm_support(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        default="",
        description="Exact version. Leave empty for the latest version.",
    ),
) -> dict:
    data = await _fetch_manifest(package_name, version)
    has, reasons = _manifest_has_esm(data)
    return {
        "package": data.get("name"),
        "version": data.get("version"),
        "esm":     has,
        "reasons": reasons or ["No ESM declarations found."],
        "type":    data.get("type", "commonjs"),
        "module":  data.get("module", ""),
    }


# ---------------------------------------------------------------------------
# TOOL 50: check_cjs_support
# ---------------------------------------------------------------------------
@mcp.tool(
    name="check_cjs_support",
    description=(
        "Check whether an npm package supports CommonJS. Looks at 'main', "
        "exports 'require' conditions, and the absence of 'type: module'."
    ),
)
async def check_cjs_support(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        default="",
        description="Exact version. Leave empty for the latest version.",
    ),
) -> dict:
    data = await _fetch_manifest(package_name, version)
    has, reasons = _manifest_has_cjs(data)
    return {
        "package": data.get("name"),
        "version": data.get("version"),
        "cjs":     has,
        "reasons": reasons or ["No CommonJS entry points found."],
        "main":    data.get("main", ""),
    }


# ---------------------------------------------------------------------------
# TOOL 51: check_typescript_support
# ---------------------------------------------------------------------------
# TypeScript support can be signalled two ways:
#   1. Built-in: package declares "types" or "typings" in its manifest, OR
#      ships .d.ts files with the package.
#   2. DefinitelyTyped: a separate @types/<name> package exists on npm.
# We check both and report which (or none).
@mcp.tool(
    name="check_typescript_support",
    description=(
        "Check if an npm package has TypeScript support: either built-in "
        "types (via 'types'/'typings'/exports .d.ts) or a DefinitelyTyped "
        "companion package (@types/<name>)."
    ),
)
async def check_typescript_support(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        default="",
        description="Exact version. Leave empty for the latest version.",
    ),
) -> dict:
    data = await _fetch_manifest(package_name, version)

    types_field  = data.get("types") or data.get("typings") or ""
    exports_field = data.get("exports")
    exports_has_types = False
    if isinstance(exports_field, dict):
        def _walk(o):
            nonlocal exports_has_types
            if isinstance(o, dict):
                for k, v in o.items():
                    if k == "types":
                        exports_has_types = True
                    _walk(v)
        _walk(exports_field)

    built_in = bool(types_field) or exports_has_types

    # Derive the @types/<name> slug — scoped packages use double-underscore:
    # @babel/core -> @types/babel__core.
    raw = package_name
    if raw.startswith("@"):
        scope, name = raw[1:].split("/", 1)
        dt_name = f"@types/{scope}__{name}"
    else:
        dt_name = f"@types/{raw}"

    # Look up the DT package — may or may not exist.
    dt_version = None
    try:
        dt = await _fetch_json(
            f"{NPM_REGISTRY}/{dt_name}/latest",
            not_found_msg="__no_dt__",
        )
        dt_version = dt.get("version")
    except ValueError:
        pass

    if built_in:
        summary = "built-in types"
    elif dt_version:
        summary = "DefinitelyTyped package available"
    else:
        summary = "no TypeScript support detected"

    return {
        "package":          data.get("name", package_name),
        "version":          data.get("version"),
        "built_in_types":   built_in,
        "types_field":      types_field,
        "exports_types":    exports_has_types,
        "definitelytyped":  dt_version is not None,
        "definitelytyped_package": dt_name if dt_version else None,
        "definitelytyped_version": dt_version,
        "summary":          summary,
    }


# ---------------------------------------------------------------------------
# TOOL 52: get_exports_map
# ---------------------------------------------------------------------------
# Returns the raw "exports" field plus a flattened list of entry points for
# quick inspection. The modern "exports" field gates what consumers can import
# from a package — important for deep-import debugging.
@mcp.tool(
    name="get_exports_map",
    description=(
        "Return the 'exports' field of an npm package's manifest plus a "
        "flattened list of entry-point subpaths (e.g. '.', './router')."
    ),
)
async def get_exports_map(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        default="",
        description="Exact version. Leave empty for the latest version.",
    ),
) -> dict:
    data = await _fetch_manifest(package_name, version)
    exports_field = data.get("exports")

    # Flatten subpaths for quick skimming. The exports field can be:
    #   - a single string (main entry)
    #   - a dict of subpaths -> (string | conditions dict)
    subpaths: list[str] = []
    if isinstance(exports_field, str):
        subpaths = ["."]
    elif isinstance(exports_field, dict):
        # Subpaths always start with "."
        subpaths = [k for k in exports_field.keys() if k.startswith(".")]

    return {
        "package":        data.get("name", package_name),
        "version":        data.get("version"),
        "exports":        exports_field,   # raw field for full inspection
        "subpaths":       subpaths,
        "subpath_count":  len(subpaths),
        "main":           data.get("main", ""),
        "module":         data.get("module", ""),
        "has_exports_map": exports_field is not None,
    }


# ---------------------------------------------------------------------------
# TOOL 53: check_browser_compatible
# ---------------------------------------------------------------------------
# Signals used:
#   - "browser" field in package.json (strongest positive signal)
#   - "exports" with a "browser" condition
#   - "engines" declaring only Node (weak negative)
#   - "main" ending in ".node" or references to native bindings (strong negative)
@mcp.tool(
    name="check_browser_compatible",
    description=(
        "Check if an npm package is meant to run in the browser. Uses "
        "'browser' field, 'exports' conditions, and engine constraints "
        "to derive a yes / likely / unlikely / no verdict."
    ),
)
async def check_browser_compatible(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        default="",
        description="Exact version. Leave empty for the latest version.",
    ),
) -> dict:
    data = await _fetch_manifest(package_name, version)

    browser_field = data.get("browser")
    has_browser_field = browser_field is not None and browser_field != {}

    exports_field = data.get("exports")
    exports_has_browser = False
    if isinstance(exports_field, dict):
        def _walk(o):
            nonlocal exports_has_browser
            if isinstance(o, dict):
                for k, v in o.items():
                    if k == "browser":
                        exports_has_browser = True
                    _walk(v)
        _walk(exports_field)

    # Strong negative signals.
    main = (data.get("main") or "").lower()
    bin_ = data.get("bin")
    uses_native = main.endswith(".node")
    has_bin = bool(bin_)

    # Engine constraints only reference Node — doesn't prove browser-hostile
    # but it's a weak hint that the authors primarily targeted Node.
    engines = data.get("engines", {}) or {}
    node_only_engines = bool(engines.get("node")) and not has_browser_field

    # Derive verdict.
    if uses_native:
        verdict = "no"
        reason = "Ships a native .node binary — browser cannot load it."
    elif has_browser_field or exports_has_browser:
        verdict = "yes"
        reason = "Declares a 'browser' field or 'browser' export condition."
    elif has_bin and not has_browser_field:
        verdict = "unlikely"
        reason = "Has a CLI ('bin' field) and no browser build."
    elif node_only_engines:
        verdict = "likely"
        reason = "No explicit browser field — may still work but not guaranteed."
    else:
        verdict = "likely"
        reason = "Pure-JS package with no Node-only markers — usually bundlable."

    return {
        "package":          data.get("name", package_name),
        "version":          data.get("version"),
        "browser_compatible": verdict,
        "reason":           reason,
        "browser_field":    browser_field,
        "exports_browser":  exports_has_browser,
        "has_native":       uses_native,
        "has_bin":          has_bin,
    }


# ---------------------------------------------------------------------------
# TOOL 54: check_deno_compatible
# ---------------------------------------------------------------------------
# Deno can import any ESM package from npm via "npm:" specifiers since v1.28.
# It can import CJS too but with caveats. The clearest signals:
#   - ESM-only or dual ESM/CJS → works well
#   - Pure CJS with no ESM build → works but with interop quirks
#   - Native modules (.node files) → doesn't work
#   - Published on JSR → first-class Deno support
@mcp.tool(
    name="check_deno_compatible",
    description=(
        "Check if an npm package is compatible with Deno. Looks at ESM/CJS "
        "support, native modules, and JSR presence."
    ),
)
async def check_deno_compatible(
    package_name: str = Field(description="Exact npm package name."),
    version: str = Field(
        default="",
        description="Exact version. Leave empty for the latest version.",
    ),
) -> dict:
    data = await _fetch_manifest(package_name, version)
    has_esm, _   = _manifest_has_esm(data)
    has_cjs, _   = _manifest_has_cjs(data)
    main = (data.get("main") or "").lower()
    uses_native = main.endswith(".node")

    # JSR presence — first-class Deno support.
    jsr = await _check_jsr(package_name)
    on_jsr = jsr.get("on_jsr", False)

    if uses_native:
        verdict = "no"
        reason = "Ships a native .node binary — Deno cannot load it."
    elif on_jsr:
        verdict = "yes"
        reason = "Published on JSR — first-class Deno support."
    elif has_esm:
        verdict = "yes"
        reason = "ESM build — Deno imports npm ESM packages natively."
    elif has_cjs:
        verdict = "likely"
        reason = "CJS-only — Deno supports via npm: specifiers but with interop caveats."
    else:
        verdict = "unknown"
        reason = "Could not determine module format."

    return {
        "package":         data.get("name", package_name),
        "version":         data.get("version"),
        "deno_compatible": verdict,
        "reason":          reason,
        "has_esm":         has_esm,
        "has_cjs":         has_cjs,
        "has_native":      uses_native,
        "on_jsr":          on_jsr,
    }


# ---------------------------------------------------------------------------
# JSR helper — shared by check_deno_compatible and get_package_on_jsr.
# ---------------------------------------------------------------------------
# JSR packages are always scoped (@scope/name). For unscoped npm packages
# we still attempt a lookup using @scope/name if the user passes one, but
# otherwise we report "not on JSR".
async def _check_jsr(package_name: str) -> dict:
    if not package_name.startswith("@") or "/" not in package_name:
        return {"on_jsr": False, "note": "JSR packages are always scoped (@scope/name)."}
    scope, name = package_name[1:].split("/", 1)
    url = f"https://api.jsr.io/scopes/{scope}/packages/{name}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(url)
    except httpx.RequestError as exc:
        return {"on_jsr": False, "error": str(exc)}
    if response.status_code == 404:
        return {"on_jsr": False}
    if response.status_code >= 400:
        return {"on_jsr": False, "error": f"HTTP {response.status_code}"}
    data = response.json()
    return {
        "on_jsr":      True,
        "jsr_url":     f"https://jsr.io/@{scope}/{name}",
        "description": data.get("description", ""),
        "latest":      data.get("latestVersion"),
        "runtimes":    data.get("runtimeCompat", {}),
    }


# ---------------------------------------------------------------------------
# TOOL 55: get_package_on_jsr
# ---------------------------------------------------------------------------
@mcp.tool(
    name="get_package_on_jsr",
    description=(
        "Check whether a package is also published on the JSR registry "
        "(jsr.io). JSR is the modern registry favoured by Deno and Bun."
    ),
)
async def get_package_on_jsr(
    package_name: str = Field(
        description="Package name, e.g. '@std/path' or '@luca/cases'."
    ),
) -> dict:
    return {
        "package": package_name,
        **(await _check_jsr(package_name)),
    }


# ---------------------------------------------------------------------------
# Run the server
# ---------------------------------------------------------------------------
# stdio transport: the MCP client spawns this script as a subprocess and
# communicates over stdin/stdout with JSON-RPC frames.
if __name__ == "__main__":
    mcp.run(transport="stdio")
