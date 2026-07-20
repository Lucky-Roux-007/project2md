#!/usr/bin/env python3
"""p2m.py - Project to Markdown exporter.

Walks a project directory and produces a single Markdown file containing a
directory tree plus the contents of every relevant source file. The output
is optimized for feeding a project to an LLM: it is hierarchical, free of
noise (build artifacts, lock files, binaries, secrets), and capped in size
so large or generated files don't blow up the token budget.

Usage:
    python3 p2m.py [path] [options]

If no path is given, the current directory is used. By default the script
interactively asks where to save the output; pass -o/--output (or --stdout)
to skip the prompt entirely and run non-interactively (e.g. from CI or a
script/alias).
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_NAME = Path(sys.argv[0]).name


def output_filename_for(root: Path) -> str:
    """e.g. project root 'fly-in' -> 'fly-in.md'"""
    return f"{root.name}.md"


# Directories whose contents are never useful to an LLM reading the codebase.
IGNORE_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "env",
    "node_modules",
    ".next",
    ".nuxt",
    "dist",
    "build",
    "target",
    "out",
    ".idea",
    ".vscode",
    "coverage",
    "htmlcov",
}

# Exact filenames to skip: lock files (huge, low signal), OS cruft.
IGNORE_FILE_NAMES = {
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
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
    ".o",
    ".obj",
    ".class",
    ".jar",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".ico",
    ".tiff",
    ".mp3",
    ".mp4",
    ".mov",
    ".avi",
    ".wav",
    ".flac",
    ".ttf",
    ".otf",
    ".woff",
    ".woff2",
    ".eot",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".rar",
    ".7z",
    ".whl",
    ".db",
    ".sqlite",
    ".sqlite3",
}

# Files larger than this are still listed but their content is truncated, so
# generated assets or datasets don't dominate the token budget. Overridable
# with --max-bytes.
DEFAULT_MAX_INLINE_BYTES = 200_000

# Ratio of unicode replacement chars (from a failed utf-8 decode) above which
# a file is treated as binary rather than garbled text.
BINARY_REPLACEMENT_RATIO = 0.05

LANGUAGE_BY_EXTENSION = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".kt": "kotlin",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".bat": "bat",
    ".ps1": "powershell",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".sql": "sql",
    ".md": "markdown",
    ".rst": "rst",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".xml": "xml",
    ".ini": "ini",
    ".cfg": "ini",
    ".txt": "text",
}

# Shebang interpreter -> language, used when a file has no extension.
SHEBANG_LANGUAGE = {
    "python": "python",
    "python3": "python",
    "bash": "bash",
    "sh": "bash",
    "zsh": "bash",
    "node": "javascript",
    "ruby": "ruby",
    "perl": "perl",
}


class CliError(Exception):
    """Raised for user-facing input problems; caught in main() for a clean exit."""


# ---------------------------------------------------------------------------
# .gitignore support (best-effort; no third-party deps)
# ---------------------------------------------------------------------------


@dataclass
class GitignoreMatcher:
    """Minimal .gitignore matcher: supports comments, blank lines, `/`-rooted
    and directory-only (`trailing/`) patterns, matched against the path
    relative to the project root. Negation (`!pattern`) is not supported and
    such lines are skipped, since correctly re-including a previously
    excluded path requires full precedence handling.
    """

    patterns: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, root: Path) -> "GitignoreMatcher":
        gitignore = root / ".gitignore"
        patterns: list[str] = []
        if gitignore.is_file():
            try:
                for line in gitignore.read_text(
                    encoding="utf-8", errors="ignore"
                ).splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("!"):
                        continue
                    patterns.append(line)
            except OSError:
                pass
        return cls(patterns)

    def matches(self, rel_posix_path: str, is_dir: bool) -> bool:
        name = rel_posix_path.rsplit("/", 1)[-1]
        for pattern in self.patterns:
            p = pattern
            dir_only = p.endswith("/")
            if dir_only:
                p = p[:-1]
            rooted = p.startswith("/")
            if rooted:
                p = p[1:]

            if "/" in p:
                # Pattern includes a path component: match against full rel path.
                target = rel_posix_path
                pat = p
            else:
                # Simple pattern: match against the basename at any depth.
                target = name
                pat = p

            if dir_only and not is_dir:
                continue

            if fnmatch.fnmatch(target, pat):
                return True
            if rooted and fnmatch.fnmatch(rel_posix_path, p):
                return True
        return False


# ---------------------------------------------------------------------------
# Filesystem scanning
# ---------------------------------------------------------------------------


@dataclass
class Node:
    """A single file or directory in the (already-filtered) project tree."""

    path: Path
    is_dir: bool
    children: list["Node"] = field(default_factory=list)


@dataclass
class ScanOptions:
    output_name: str
    gitignore: Optional[GitignoreMatcher] = None
    extra_excludes: tuple[str, ...] = ()
    include_exts: Optional[frozenset[str]] = None
    follow_symlinks: bool = False


def should_ignore_dir(name: str) -> bool:
    return name in IGNORE_DIR_NAMES or name.endswith(".egg-info")


def should_ignore_file(path: Path, options: ScanOptions) -> bool:
    name = path.name
    if name == options.output_name:
        return True
    if name in IGNORE_FILE_NAMES:
        return True
    if name.startswith(IGNORE_FILE_PREFIXES):
        return True
    if path.suffix.lower() in IGNORE_EXTENSIONS:
        return True
    if (
        options.include_exts is not None
        and path.suffix.lower() not in options.include_exts
    ):
        return True
    return False


def matches_extra_exclude(rel_posix: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(rel_posix, pat) for pat in patterns)


def scan_project(root: Path, options: ScanOptions) -> Node:
    """Recursively build a filtered tree of the project, dirs first then alpha."""

    visited_real_dirs: set[Path] = set()

    def scan_dir(directory: Path) -> list[Node]:
        try:
            entries = sorted(
                directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
            )
        except PermissionError:
            return []

        nodes = []
        for entry in entries:
            if entry.is_symlink() and not options.follow_symlinks:
                continue  # avoid duplicated content and symlink loops

            rel_posix = entry.relative_to(root).as_posix()

            if entry.is_dir():
                if should_ignore_dir(entry.name):
                    continue
                if options.gitignore and options.gitignore.matches(rel_posix, True):
                    continue
                if matches_extra_exclude(rel_posix, options.extra_excludes):
                    continue
                if entry.is_symlink():
                    real = entry.resolve()
                    if real in visited_real_dirs:
                        continue  # symlink loop guard
                    visited_real_dirs.add(real)
                nodes.append(Node(entry, True, scan_dir(entry)))
            elif entry.is_file():
                if should_ignore_file(entry, options):
                    continue
                if options.gitignore and options.gitignore.matches(rel_posix, False):
                    continue
                if matches_extra_exclude(rel_posix, options.extra_excludes):
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


def detect_shebang_language(raw: bytes) -> str:
    if not raw.startswith(b"#!"):
        return ""
    first_line = raw.split(b"\n", 1)[0].decode("utf-8", errors="ignore")
    interpreter = (
        first_line.rsplit("/", 1)[-1].split()[0] if first_line[2:].strip() else ""
    )
    return SHEBANG_LANGUAGE.get(interpreter, "")


def load_file_content(path: Path, max_inline_bytes: int) -> FileContent:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return FileContent(
            text=None, error=f"could not read file ({exc.strerror or exc})"
        )

    if b"\x00" in raw:
        return FileContent(text=None, error="binary file, content omitted")

    truncated = len(raw) > max_inline_bytes
    payload = raw[:max_inline_bytes] if truncated else raw
    text = payload.decode("utf-8", errors="replace")

    # A high ratio of replacement chars means this wasn't really utf-8 text
    # (e.g. UTF-16, or a binary format without null bytes) — skip it rather
    # than dumping garbage into the export.
    if payload:
        replacement_ratio = text.count("\ufffd") / len(payload)
        if replacement_ratio > BINARY_REPLACEMENT_RATIO:
            return FileContent(text=None, error="binary file, content omitted")

    return FileContent(text=text, truncated=truncated)


def write_file_section(out, root: Path, path: Path, max_inline_bytes: int) -> None:
    rel = path.relative_to(root).as_posix()
    out.write(f"### `{rel}`\n\n")

    content = load_file_content(path, max_inline_bytes)
    if content.error:
        out.write(f"*{content.error}*\n\n")
        return

    if not content.text:
        out.write("*(empty file)*\n\n")
        return

    lang = LANGUAGE_BY_EXTENSION.get(path.suffix.lower(), "")
    if not lang:
        lang = detect_shebang_language(content.text.encode("utf-8", errors="ignore"))
    out.write(f"```{lang}\n{content.text}")
    if not content.text.endswith("\n"):
        out.write("\n")
    out.write("```\n\n")

    if content.truncated:
        out.write(
            f"*(truncated — file exceeds {max_inline_bytes:,} bytes; showing the first portion only)*\n\n"
        )


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def estimate_tokens(char_count: int) -> str:
    """Rough order-of-magnitude estimate (~4 chars/token); good enough for
    gauging whether an export will blow a model's context budget."""
    tokens = char_count // 4
    if tokens >= 1_000_000:
        return f"~{tokens / 1_000_000:.1f}M tokens"
    if tokens >= 1_000:
        return f"~{tokens / 1_000:.1f}K tokens"
    return f"~{tokens} tokens"


# ---------------------------------------------------------------------------
# Document assembly
# ---------------------------------------------------------------------------


@dataclass
class ExportStats:
    file_count: int
    total_bytes: int
    char_count: int


def generate_project_md(
    root: Path, out, options: ScanOptions, max_inline_bytes: int
) -> ExportStats:
    """Scan `root` and write the full LLM-oriented Markdown export to `out`."""
    tree = scan_project(root, options)
    files = list(iter_files(tree))
    total_bytes = sum(f.stat().st_size for f in files)

    out.write(f"# Project Export: {root.name}\n\n")
    out.write(
        "Generated by p2m.py — a structure and source snapshot formatted for LLM analysis.\n\n"
    )

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
    char_count = 0
    for path in files:
        section_start = out.tell() if out.seekable() else 0
        write_file_section(out, root, path, max_inline_bytes)
        if out.seekable():
            char_count += out.tell() - section_start

    return ExportStats(
        file_count=len(files), total_bytes=total_bytes, char_count=char_count
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=SCRIPT_NAME,
        description="Export a project's structure and source files into a single, LLM-friendly Markdown file.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Path to the project directory (default: current directory).",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help="Output file or directory. Skips the interactive prompt. "
        "If a directory, the file is named <project>.md inside it.",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Write the export to stdout instead of a file (skips the prompt).",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_INLINE_BYTES,
        metavar="N",
        help=f"Truncate file contents beyond N bytes (default: {DEFAULT_MAX_INLINE_BYTES:,}).",
    )
    parser.add_argument(
        "--include",
        nargs="+",
        metavar="EXT",
        help="Only include files with these extensions (e.g. --include .py .md).",
    )
    parser.add_argument(
        "--exclude",
        nargs="+",
        metavar="PATTERN",
        default=[],
        help="Additional glob pattern(s) to exclude, matched against the path "
        "relative to the project root (e.g. --exclude 'tests/*' '*.generated.*').",
    )
    parser.add_argument(
        "--no-gitignore",
        action="store_true",
        help="Don't apply the project's .gitignore rules.",
    )
    parser.add_argument(
        "--follow-symlinks",
        action="store_true",
        help="Follow symlinked files and directories (loop-guarded).",
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
    """Interactively ask the user where the output file should be saved."""
    print(f"\nWhere would you like to save {output_filename_for(project_root)}?\n")
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
                custom = input(
                    "Enter the output directory path (blank to cancel): "
                ).strip()
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


def resolve_output_path(project_root: Path, output_arg: str) -> Path:
    candidate = Path(output_arg).expanduser().resolve()
    if candidate.is_dir():
        return candidate / output_filename_for(project_root)
    return candidate


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    try:
        project_root = resolve_project_root(args.path)
    except CliError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    include_exts = None
    if args.include:
        include_exts = frozenset(
            (e if e.startswith(".") else f".{e}").lower() for e in args.include
        )

    gitignore = None if args.no_gitignore else GitignoreMatcher.load(project_root)

    scan_options = ScanOptions(
        output_name=output_filename_for(project_root),
        gitignore=gitignore,
        extra_excludes=tuple(args.exclude),
        include_exts=include_exts,
        follow_symlinks=args.follow_symlinks,
    )

    if args.stdout:
        stats = generate_project_md(
            project_root, sys.stdout, scan_options, args.max_bytes
        )
        print(
            f"\n✓ Exported {stats.file_count} files ({human_size(stats.total_bytes)}, "
            f"{estimate_tokens(stats.char_count)}) to stdout",
            file=sys.stderr,
        )
        return 0

    print(f"Project directory: {project_root}")

    if args.output:
        output_path = resolve_output_path(project_root, args.output)
    else:
        try:
            output_dir = prompt_output_directory(project_root)
        except EOFError:
            print("\nCancelled.")
            return 130
        output_path = output_dir / output_filename_for(project_root)

    print(f"\nScanning project and writing {output_path.name}...")
    try:
        with output_path.open("w", encoding="utf-8") as out:
            stats = generate_project_md(project_root, out, scan_options, args.max_bytes)
    except PermissionError:
        print(f"Error: no permission to write to {output_path}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Error: could not write to {output_path}: {exc}", file=sys.stderr)
        return 1

    print(
        f"✓ Exported {stats.file_count} files ({human_size(stats.total_bytes)}, "
        f"{estimate_tokens(stats.char_count)}) to {output_path}"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
