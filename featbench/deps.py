"""Parse dependency manifests from common ecosystems into sets of package names."""

from __future__ import annotations

import json
import posixpath
import re
import tomllib

_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def is_manifest(path: str) -> bool:
    base = posixpath.basename(path)
    return (base in {"pyproject.toml", "package.json", "go.mod", "Cargo.toml", "pom.xml",
                     "build.gradle", "build.gradle.kts", "Gemfile", "composer.json", "setup.cfg"}
            or bool(re.match(r"requirements.*\.txt$", base))
            or base.endswith(".csproj"))


def _req_names(lines) -> set[str]:
    names = set()
    for line in lines:
        line = line.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        m = _REQ_NAME.match(line)
        if m:
            names.add(m.group(1).lower().replace("_", "-"))
    return names


def parse_manifest(path: str, text: str) -> set[str]:
    base = posixpath.basename(path)
    try:
        if base.startswith("requirements") and base.endswith(".txt"):
            return _req_names(text.splitlines())
        if base == "pyproject.toml":
            data = tomllib.loads(text)
            names = _req_names(data.get("project", {}).get("dependencies", []))
            for group in data.get("project", {}).get("optional-dependencies", {}).values():
                names |= _req_names(group)
            poetry = data.get("tool", {}).get("poetry", {})
            names |= {k.lower() for k in poetry.get("dependencies", {}) if k.lower() != "python"}
            names |= {k.lower() for k in poetry.get("dev-dependencies", {})}
            return names
        if base == "package.json":
            data = json.loads(text)
            names = set()
            for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                names |= set(data.get(key, {}) or {})
            return names
        if base == "composer.json":
            data = json.loads(text)
            return set(data.get("require", {})) | set(data.get("require-dev", {}))
        if base == "go.mod":
            names = set()
            in_block = False
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("require ("):
                    in_block = True
                elif in_block and s == ")":
                    in_block = False
                elif in_block and s and not s.startswith("//"):
                    names.add(s.split()[0])
                elif s.startswith("require "):
                    names.add(s.split()[1])
            return names
        if base == "Cargo.toml":
            data = tomllib.loads(text)
            names = set()
            for key in ("dependencies", "dev-dependencies", "build-dependencies"):
                names |= set(data.get(key, {}))
            return names
        if base == "pom.xml":
            return {f"{g}:{a}" for g, a in re.findall(
                r"<dependency>.*?<groupId>(.*?)</groupId>.*?<artifactId>(.*?)</artifactId>", text, re.S)}
        if base.startswith("build.gradle"):
            return set(re.findall(
                r"""(?:implementation|api|compileOnly|runtimeOnly|testImplementation|kapt|ksp)\s*\(?\s*['"]([^:'"]+:[^:'"]+)""", text))
        if base == "Gemfile":
            return set(re.findall(r"""^\s*gem\s+['"]([^'"]+)['"]""", text, re.M))
        if base.endswith(".csproj"):
            return set(re.findall(r'<PackageReference\s+Include="([^"]+)"', text))
        if base == "setup.cfg":
            m = re.search(r"install_requires\s*=\s*\n((?:[ \t]+.+\n?)+)", text)
            return _req_names(m.group(1).splitlines()) if m else set()
    except (ValueError, tomllib.TOMLDecodeError):
        return set()
    return set()


def collect(paths: list[str], read) -> set[str]:
    """Union of dependencies declared in all manifests among `paths`; `read(path) -> str|None`."""
    names: set[str] = set()
    for p in paths:
        if is_manifest(p):
            text = read(p)
            if text:
                names |= parse_manifest(p, text)
    return names
