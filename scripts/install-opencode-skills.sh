#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILLS_SOURCE_DIR="$REPO_DIR/plugins/pgmac-workflows/skills"
SKILLS_TARGET_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/skills"

SKILL_NAMES=(
    create-pir
    pickup-ticket
    grilling
    domain-modeling
)

show_help() {
    cat <<EOF
Usage: $(basename "$0") [OPTION]

Install or uninstall opencode skills from this repository.

Options:
  --uninstall    Remove symlinked skills from the global skills directory
  --help         Show this help message

Without options, installs skills to:
  $SKILLS_TARGET_DIR
EOF
    exit 0
}

do_uninstall() {
    local count=0
    for name in "${SKILL_NAMES[@]}"; do
        target="$SKILLS_TARGET_DIR/$name"
        if [ -L "$target" ]; then
            rm "$target"
            echo "Removed: $target"
            ((count++))
        fi
    done
    if [ "$count" -eq 0 ]; then
        echo "No installed opencode skills found."
    else
        echo "Uninstalled $count skill(s). Restart opencode for the change to take effect."
    fi
    exit 0
}

# Parse arguments
for arg in "$@"; do
    case "$arg" in
        --help) show_help ;;
        --uninstall) do_uninstall ;;
        *)
            echo "Unknown option: $arg"
            echo "Usage: $(basename "$0") [--help] [--uninstall]"
            exit 1
            ;;
    esac
done

# Verify source directories exist
for name in "${SKILL_NAMES[@]}"; do
    if [ ! -d "$SKILLS_SOURCE_DIR/$name" ]; then
        echo "Error: source skill directory not found: $SKILLS_SOURCE_DIR/$name"
        exit 1
    fi
done

# Create target directory
mkdir -p "$SKILLS_TARGET_DIR"

# Install each skill
count=0
for name in "${SKILL_NAMES[@]}"; do
    source_skill="$SKILLS_SOURCE_DIR/$name"
    target_skill="$SKILLS_TARGET_DIR/$name"

    if [ -e "$target_skill" ] || [ -L "$target_skill" ]; then
        if [ ! -L "$target_skill" ]; then
            echo "Warning: $target_skill exists and is not a symlink. Skipping $name."
            continue
        fi
        rm "$target_skill"
    fi

    ln -s "$source_skill" "$target_skill"
    echo "Installed: $name"
    count=$((count + 1))
done

echo ""
echo "Installed $count skill(s) to $SKILLS_TARGET_DIR"
echo "Restart opencode for the skills to become available."
