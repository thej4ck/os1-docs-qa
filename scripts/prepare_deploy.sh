#!/bin/bash
# Prepare the app for deployment:
# 1. Builds search.db from the docs repo
# 2. Copies help images into the project
#
# Usage: ./scripts/prepare_deploy.sh [DOCS_REPO_PATH]
# Default: ../os1-documentation/Claude Code Playground

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DOCS_REPO="${1:-$PROJECT_DIR/../os1-documentation/Claude Code Playground}"

echo "=== OS1 Docs Q&A — Deploy Preparation ==="
echo "Project:  $PROJECT_DIR"
echo "Docs repo: $DOCS_REPO"
echo

# 1. Build search index
echo "Building search index..."
cd "$PROJECT_DIR"
python scripts/build_index.py --repo "$DOCS_REPO" --db data/search.db
echo

# 2. Copy help images
echo "Copying help files (images)..."
rm -rf "$PROJECT_DIR/help-files"
cp -r "$DOCS_REPO/sources/help" "$PROJECT_DIR/help-files"
echo "Help files copied: $(find "$PROJECT_DIR/help-files" -type f | wc -l) files"
echo

echo "=== Done! Ready to deploy. ==="
echo "  data/search.db  — search index (baked into image)"
echo "  help-files/      — HTML help + images (baked into image)"
echo "  data/app.db      — user data (persistent volume, NOT in image)"
