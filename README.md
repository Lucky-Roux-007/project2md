# p2m — Project to Markdown

`p2m` walks a project directory and exports it into a single, well-structured
Markdown file: a directory tree plus the contents of every relevant source
file. It's built for feeding a whole codebase to an LLM in one paste —
noise-free, hierarchical, and capped in size so large or generated files
don't blow up your token budget.

## Features

- **Directory tree** rendered in a familiar `tree`-style connector format.
- **Smart filtering**, skipped automatically:
  - VCS and tooling dirs: `.git`, `__pycache__`, `.mypy_cache`, `.venv`, `node_modules`, `dist`, `build`, `.idea`, `coverage`, etc.
  - Lock files: `package-lock.json`, `yarn.lock`, `poetry.lock`, `Cargo.lock`, and similar.
  - Secrets: any file starting with `.env`.
  - Binary/media files: images, audio, video, fonts, archives, databases, compiled binaries.
  - OS cruft: `.DS_Store`, `Thumbs.db`.
- **Size-capped output** — files over 200 KB are still listed but truncated, so one giant generated file can't dominate the export.
- **Symlinks skipped** to avoid loops and duplicated content.
- **Language-aware code fences** (Python, JS/TS, Go, Rust, Java, C/C++, shell, SQL, and more) for clean syntax highlighting in the output.
- Single dependency: **Python 3 standard library only**, no pip packages required.

## Requirements

- macOS or Linux (or WSL on Windows)
- `bash`
- `python3`

## Installation

Clone or download this repo, then from inside the project folder run:

```bash
bash install.sh
```

This will:

1. Verify `p2m.py` is present and is valid Python.
2. Copy it to `~/.local/bin/p2m` and make it executable (backing up any existing binary at that path first).
3. Add `~/.local/bin` to your `PATH` by appending a small, idempotent block to your shell config (`~/.bashrc` or `~/.zshrc`, detected from `$SHELL`) — safe to run more than once, it won't duplicate the entry.

If `p2m` isn't picked up immediately, open a new terminal or run:

```bash
source ~/.bashrc   # or ~/.zshrc
```

> **Note:** the installer must be run with `bash` (`bash install.sh`), not `sh install.sh`. It also expects `p2m.py` to be next to `install.sh` (either in your current directory or the script's own directory) — it does not fetch it over the network, so a bare `curl .../install.sh | bash` with nothing else downloaded won't work. Grab both files (e.g. `git clone` this repo) and run the installer from that folder.

### Uninstalling

```bash
bash install.sh --uninstall
```

Removes `~/.local/bin/p2m` and cleans up the PATH block from your shell config.

## Usage

```bash
p2m [path]
```

- `path` is optional and defaults to the current directory.
- You'll be prompted for where to save the output:
  1. The project directory itself
  2. Your home directory
  3. A custom path you type in

The result is written as `project.md` in the chosen location.

### Example

```bash
$ p2m ~/code/my-app
Project directory: /Users/you/code/my-app

Where would you like to save project.md?

  1) Current project directory
  2) Home directory ($HOME)
  3) Custom path

Choice [1-3]: 1

Scanning project and writing project.md...
✓ Exported 42 files (186.4 KB) to /Users/you/code/my-app/project.md
```

You can then paste `project.md` directly into an LLM chat, or upload it as
context for code review, refactoring help, documentation generation, etc.

## Output format

`project.md` contains three sections:

- **Overview** — root path, file count, total size.
- **Structure** — a `tree`-style diagram of the filtered project.
- **Files** — every included file's relative path as a heading, followed by
  its contents in a fenced, language-tagged code block. Empty files, unreadable
  files, and binaries are noted inline instead of dumped.

## Manual installation (without install.sh)

If you'd rather not run the installer:

```bash
mkdir -p ~/.local/bin
cp p2m.py ~/.local/bin/p2m
chmod +x ~/.local/bin/p2m
export PATH="$HOME/.local/bin:$PATH"   # add this line to your shell config too
```

## License

Add your license of choice here (e.g. MIT).
