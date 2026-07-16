#!/usr/bin/env python3
"""p2m.py - Project to Markdown exporter.

Walks a project directory and produces a single Markdown file containing a
directory tree plus the contents of every relevant source file. The output
is optimized for feeding a project to an LLM: it is hierarchical, free of
noise (build artifacts, lock files, binaries, secrets), and capped in size
so large or generated files don't blow up the token budget.

Usage:
    python3 p2m.py [path]

If no path is given, the current directory is used. The script then asks
where to save the resulting project.md.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_NAME = Path(sys.argv[0]).name
OUTPUT_FILENAME = "project.md"

# Directories whose contents are never useful to an LLM reading the codebase.
IGNORE_DIR_NAMES = {
    ".git", ".hg", ".svn",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    ".venv", "venv", "env",
    "node_modules", ".next", ".nuxt",
    "dist", "build", "target", "out",
    ".idea", ".vscode",
    "coverage", "htmlcov",
}

# Exact filenames to skip: lock files (huge, low signal), OS cruft.
IGNORE_FILE_NAMES = {
    OUTPUT_FILENAME,
    SCRIPT_NAME,
    ".DS_Store",
    "Thumbs.db",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
}

# Filename prefixes that typically hold secrets and shouldn't be exported.
IGNORE_FILE_PREFIXES = (".env",)

# Extensions with no useful text representation.
IGNORE_EXTENSIONS = {
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib", ".exe", ".o", ".obj", ".class", ".jar",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".tiff",
    ".mp3", ".mp4", ".mov", ".avi", ".wav", ".flac",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".pdf", ".zip", ".tar", ".gz", ".rar", ".7z", ".whl",
    ".db", ".sqlite", ".sqlite3",
}

# Files larger than this are still listed but their content is truncated, so
# generated assets or datasets don't dominate the token budget.
MAX_INLINE_BYTES = 200_000

LANGUAGE_BY_EXTENSION = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".java": "java", ".kt": "kotlin", ".go": "go", ".rs": "rust",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".bat": "bat", ".ps1": "powershell",
    ".html": "html", ".css": "css", ".scss": "scss", ".sql": "sql",
    ".md": "markdown", ".rst": "rst",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".xml": "xml", ".ini": "ini", ".cfg": "ini", ".txt": "text",
}


class CliError(Exception):
    """Raised for user-facing input problems; caught in main() for a clean exit."""


# ---------------------------------------------------------------------------
# Filesystem scanning
# ---------------------------------------------------------------------------


@dataclass
class Node:
    """A single file or directory in the (already-filtered) project tree."""

    path: Path
    is_dir: bool
    children: list["Node"] = field(default_factory=list)


def should_ignore_dir(name: str) -> bool:
    return name in IGNORE_DIR_NAMES or name.endswith(".egg-info")


def should_ignore_file(path: Path) -> bool:
    name = path.name
    if name in IGNORE_FILE_NAMES:
        return True
    if name.startswith(IGNORE_FILE_PREFIXES):
        return True
    return path.suffix.lower() in IGNORE_EXTENSIONS


def scan_project(root: Path) -> Node:
    """Recursively build a filtered tree of the project, dirs first then alpha."""

    def scan_dir(directory: Path) -> list[Node]:
        try:
            entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except PermissionError:
            return []

        nodes = []
        for entry in entries:
            if entry.is_symlink():
                continue  # avoid duplicated content and symlink loops
            if entry.is_dir():
                if should_ignore_dir(entry.name):
                    continue
                nodes.append(Node(entry, True, scan_dir(entry)))
            elif entry.is_file():
                if should_ignore_file(entry):
                    continue
                nodes.append(Node(entry, False))
        return nodes

    return Node(root, True, scan_dir(root))


def iter_files(node: Node) -> Iterator[Path]:
    """Yield every file in the tree, in the same order it will be rendered."""
    for child in node.children:
        if child.is_dir:
            yield from iter_files(child)
        else:
            yield child.path


def render_tree(node: Node, prefix: str = "") -> list[str]:
    """Render a `tree`-style connector diagram of the filtered project."""
    lines = []
    children = node.children
    for index, child in enumerate(children):
        is_last = index == len(children) - 1
        connector = "└── " if is_last else "├── "
        label = f"{child.path.name}/" if child.is_dir else child.path.name
        lines.append(f"{prefix}{connector}{label}")
        if child.is_dir:
            extension = "    " if is_last else "│   "
            lines.extend(render_tree(child, prefix + extension))
    return lines


# ---------------------------------------------------------------------------
# File content rendering
# ---------------------------------------------------------------------------


@dataclass
class FileContent:
    text: Optional[str]
    truncated: bool = False
    error: Optional[str] = None


def load_file_content(path: Path) -> FileContent:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return FileContent(text=None, error=f"could not read file ({exc.strerror or exc})")

    if b"\x00" in raw:
        return FileContent(text=None, error="binary file, content omitted")

    truncated = len(raw) > MAX_INLINE_BYTES
    payload = raw[:MAX_INLINE_BYTES] if truncated else raw
    text = payload.decode("utf-8", errors="replace")
    return FileContent(text=text, truncated=truncated)


def write_file_section(out, root: Path, path: Path) -> None:
    rel = path.relative_to(root).as_posix()
    out.write(f"### `{rel}`\n\n")

    content = load_file_content(path)
    if content.error:
        out.write(f"*{content.error}*\n\n")
        return

    if not content.text:
        out.write("*(empty file)*\n\n")
        return

    lang = LANGUAGE_BY_EXTENSION.get(path.suffix.lower(), "")
    out.write(f"```{lang}\n{content.text}")
    if not content.text.endswith("\n"):
        out.write("\n")
    out.write("```\n\n")

    if content.truncated:
        out.write(f"*(truncated — file exceeds {MAX_INLINE_BYTES:,} bytes; showing the first portion only)*\n\n")


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# ---------------------------------------------------------------------------
# Document assembly
# ---------------------------------------------------------------------------


@dataclass
class ExportStats:
    file_count: int
    total_bytes: int


def generate_project_md(root: Path, output_path: Path) -> ExportStats:
    """Scan `root` and write the full LLM-oriented Markdown export to `output_path`."""
    tree = scan_project(root)
    files = list(iter_files(tree))
    total_bytes = sum(f.stat().st_size for f in files)

    with output_path.open("w", encoding="utf-8") as out:
        out.write(f"# Project Export: {root.name}\n\n")
        out.write("Generated by p2m.py — a structure and source snapshot formatted for LLM analysis.\n\n")

        out.write("## Overview\n\n")
        out.write(f"- **Root:** `{root}`\n")
        out.write(f"- **Files included:** {len(files)}\n")
        out.write(f"- **Total size:** {human_size(total_bytes)}\n\n")

        out.write("## Structure\n\n```text\n")
        out.write(f"{root.name}/\n")
        for line in render_tree(tree):
            out.write(f"{line}\n")
        out.write("```\n\n")

        out.write("## Files\n\n")
        for path in files:
            write_file_section(out, root, path)

    return ExportStats(file_count=len(files), total_bytes=total_bytes)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=SCRIPT_NAME,
        description="Export a project's structure and source files into a single, LLM-friendly project.md.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Path to the project directory (default: current directory).",
    )
    return parser.parse_args(argv)


def resolve_project_root(raw_path: str) -> Path:
    root = Path(raw_path).expanduser().resolve()
    if not root.exists():
        raise CliError(f"path does not exist: {root}")
    if not root.is_dir():
        raise CliError(f"not a directory: {root}")
    if not os.access(root, os.R_OK):
        raise CliError(f"no permission to read directory: {root}")
    return root


def prompt_output_directory(project_root: Path) -> Path:
    """Interactively ask the user where project.md should be saved."""
    print("\nWhere would you like to save project.md?\n")
    print("  1) Current project directory")
    print("  2) Home directory ($HOME)")
    print("  3) Custom path")

    while True:
        choice = input("\nChoice [1-3]: ").strip()

        if choice == "1":
            return project_root

        if choice == "2":
            return Path.home()

        if choice == "3":
            while True:
                custom = input("Enter the output directory path (blank to cancel): ").strip()
                if not custom:
                    break  # back to the main menu
                candidate = Path(custom).expanduser().resolve()
                if not candidate.exists():
                    print(f"✗ Path does not exist: {candidate}")
                elif not candidate.is_dir():
                    print(f"✗ Not a directory: {candidate}")
                else:
                    return candidate
            continue

        print("✗ Please enter 1, 2, or 3.")


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    try:
        project_root = resolve_project_root(args.path)
    except CliError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Project directory: {project_root}")

    try:
        output_dir = prompt_output_directory(project_root)
    except EOFError:
        print("\nCancelled.")
        return 130

    output_path = output_dir / OUTPUT_FILENAME

    print("\nScanning project and writing project.md...")
    try:
        stats = generate_project_md(project_root, output_path)
    except PermissionError:
        print(f"Error: no permission to write to {output_path}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Error: could not write to {output_path}: {exc}", file=sys.stderr)
        return 1

    print(f"✓ Exported {stats.file_count} files ({human_size(stats.total_bytes)}) to {output_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
