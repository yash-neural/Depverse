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


# ---------------------------------------------------------------------------
# Run the server
# ---------------------------------------------------------------------------
# stdio transport: the MCP client spawns this script as a subprocess and
# communicates over stdin/stdout with JSON-RPC frames.
if __name__ == "__main__":
    mcp.run(transport="stdio")
