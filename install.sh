#!/usr/bin/env bash
#
# install.sh - installer for p2m (Project to Markdown exporter)
#
# Usage:
#   ./install.sh              install p2m
#   ./install.sh --uninstall  remove p2m and its PATH entry
#
# Must be run with bash (not sh/dash). Safe to re-run.

# ---------------------------------------------------------------------------
# Guard: must run under bash, not sh/dash/etc.
# ---------------------------------------------------------------------------
if [ -z "${BASH_VERSION:-}" ]; then
    echo "Error: this installer requires bash. Run it as:" >&2
    echo "    bash install.sh" >&2
    exit 1
fi

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_NAME="p2m.py"
TARGET_DIR="$HOME/.local/bin"
TARGET="$TARGET_DIR/p2m"

# Resolve the directory this installer lives in, so it works whether the
# user runs `./install.sh` from elsewhere or `bash /some/path/install.sh`.
# (This does NOT make `curl url/install.sh | bash` work, since a piped
# script has no path on disk -- that mode would need p2m.py fetched
# separately. We fail with a clear message in that case instead of a
# confusing missing-file error.)
if [[ -n "${BASH_SOURCE:-}" && "${BASH_SOURCE[0]}" != "bash" && "${BASH_SOURCE[0]}" != "/dev/stdin" ]]; then
    INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
else
    INSTALLER_DIR=""
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
error_exit() {
    printf '\033[31mError:\033[0m %s\n' "$1" >&2
    exit 1
}

success_msg() {
    printf '\033[32m%s\033[0m\n' "$1"
}

info_msg() {
    printf '%s\n' "$1"
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
do_uninstall() {
    local removed=0

    if [[ -e "$TARGET" ]]; then
        rm -f "$TARGET" || error_exit "Failed to remove $TARGET."
        success_msg "Removed $TARGET"
        removed=1
    fi

    for RC in "$HOME/.bashrc" "$HOME/.zshrc"; do
        if [[ -f "$RC" ]] && grep -q 'Added by p2m installer' "$RC" 2>/dev/null; then
            # Remove the block we added (marker line + the 3 lines after it,
            # matching exactly what install writes below).
            local tmp
            tmp="$(mktemp)"
            awk '
                /# Added by p2m installer/ { skip = 4 }
                skip > 0 { skip--; next }
                { print }
            ' "$RC" > "$tmp" && mv "$tmp" "$RC"
            success_msg "Cleaned up PATH entry in $RC"
            removed=1
        fi
    done

    if [[ "$removed" -eq 0 ]]; then
        info_msg "Nothing to uninstall -- p2m was not found."
    else
        info_msg "Uninstall complete. Restart your terminal (or re-source your shell config) to fully apply."
    fi
    exit 0
}

# ---------------------------------------------------------------------------
# Main Execution
# ---------------------------------------------------------------------------
main() {
    if [[ "${1:-}" == "--uninstall" || "${1:-}" == "-u" ]]; then
        do_uninstall
    fi

    # 1. Locate p2m.py: prefer the current directory, then the directory
    #    this installer script lives in.
    local source_script=""
    if [[ -f "$SCRIPT_NAME" ]]; then
        source_script="$SCRIPT_NAME"
    elif [[ -n "$INSTALLER_DIR" && -f "$INSTALLER_DIR/$SCRIPT_NAME" ]]; then
        source_script="$INSTALLER_DIR/$SCRIPT_NAME"
    else
        error_exit "'$SCRIPT_NAME' not found in the current directory or alongside install.sh. Download both files together and run this script from that folder."
    fi

    if [[ ! -s "$source_script" ]]; then
        error_exit "'$source_script' is empty."
    fi

    if ! command -v python3 >/dev/null 2>&1; then
        error_exit "Python 3 is required but not installed or not in PATH."
    fi

    # Sanity check: make sure the file is actually valid Python before we
    # install it as an executable.
    if ! python3 -m py_compile "$source_script" 2>/dev/null; then
        error_exit "'$source_script' does not look like valid Python. Aborting install."
    fi
    rm -rf __pycache__ 2>/dev/null || true

    # 2. Setup directory and copy
    mkdir -p "$TARGET_DIR" || error_exit "Could not create directory $TARGET_DIR."

    if [[ ! -w "$TARGET_DIR" ]]; then
        error_exit "No write permission for $TARGET_DIR."
    fi

    # Back up an existing install/binary rather than silently clobbering it.
    if [[ -e "$TARGET" && ! -L "$TARGET" ]]; then
        local backup="${TARGET}.bak.$(date +%Y%m%d%H%M%S)"
        cp "$TARGET" "$backup" 2>/dev/null || true
        info_msg "Existing $TARGET found -- backed up to $backup"
    fi

    cp "$source_script" "$TARGET" || error_exit "Failed to copy '$source_script' to '$TARGET'."
    chmod +x "$TARGET" || error_exit "Failed to make '$TARGET' executable."

    # 3. Detect shell config
    #
    # NOTE: We cannot use $BASH_VERSION / $ZSH_VERSION here. This script's
    # shebang is bash, so it *always* runs under bash and $BASH_VERSION is
    # always set -- even when the user's actual default shell is zsh. That
    # would cause the PATH block to be written to ~/.bashrc for zsh users,
    # who would never source it. $SHELL is set by the OS at login and
    # reflects the user's real default shell, so use that instead, with a
    # couple of fallbacks for edge cases (e.g. $SHELL unset in some
    # containers/CI environments).
    local RC=""
    case "${SHELL:-}" in
        */zsh)
            RC="$HOME/.zshrc"
            ;;
        */bash)
            RC="$HOME/.bashrc"
            ;;
        *)
            if [[ -n "${ZSH_NAME:-}" ]]; then
                RC="$HOME/.zshrc"
            elif [[ -f "$HOME/.zshrc" && ! -f "$HOME/.bashrc" ]]; then
                RC="$HOME/.zshrc"
            else
                RC="$HOME/.bashrc"
            fi
            ;;
    esac

    # 4. Safely inject PATH variable (prevents duplicates on reload)
    local SAFE_PATH_BLOCK
    read -r -d '' SAFE_PATH_BLOCK << 'EOF' || true

# Added by p2m installer
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    export PATH="$HOME/.local/bin:$PATH"
fi
EOF

    if ! grep -qF 'Added by p2m installer' "$RC" 2>/dev/null; then
        if [[ -w "$RC" || ! -e "$RC" ]]; then
            printf '%s\n' "$SAFE_PATH_BLOCK" >> "$RC"
        else
            info_msg "Warning: could not write to $RC (no permission). Add ~/.local/bin to your PATH manually."
        fi
    fi

    # Temporarily export for the remainder of this session check
    export PATH="$HOME/.local/bin:$PATH"

    # 5. Output success and instructions
    echo
    success_msg "✅ p2m installed successfully!"
    echo
    echo "Location: $TARGET"
    echo

    if command -v p2m >/dev/null 2>&1; then
        echo "You can now run:"
        echo
        echo "    p2m"
        echo "    p2m /path/to/project"
    else
        echo "To start using it, open a new terminal or run:"
        echo
        echo "    source \"$RC\""
        echo
        echo "Then use:"
        echo
        echo "    p2m"
    fi
    echo
    echo "To uninstall later: bash install.sh --uninstall"
}

main "$@"