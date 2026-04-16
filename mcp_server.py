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


# ---------------------------------------------------------------------------
# Run the server
# ---------------------------------------------------------------------------
# stdio transport: the MCP client spawns this script as a subprocess and
# communicates over stdin/stdout with JSON-RPC frames.
if __name__ == "__main__":
    mcp.run(transport="stdio")
