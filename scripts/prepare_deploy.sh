#!/bin/bash
# Prepare the app for deployment: builds searchdata/search.db from the docs repo.
# build_index.py also refreshes help-files/ (help HTML + WebP images + schede images),
# so nothing else is needed here.
#
# Usage: ./scripts/prepare_deploy.sh [DOCS_REPO_PATH]
# Default: ../os1-documentation/Claude Code Playground

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DOCS_REPO="${1:-$PROJECT_DIR/../os1-documentation/Claude Code Playground}"

echo "=== OS1 Virgilio — Deploy Preparation ==="
echo "Project:   $PROJECT_DIR"
echo "Docs repo: $DOCS_REPO"
echo

cd "$PROJECT_DIR"
PYTHONIOENCODING=utf-8 python scripts/build_index.py --repo "$DOCS_REPO"

echo
echo "=== Done! ==="
echo "  searchdata/search.db — search index (committed, baked into image)"
echo "  help-files/          — HTML help + images (committed, baked into image)"
echo "  data/app.db          — user data (persistent volume, NOT in image)"
echo "If the docs gained new images: python scripts/describe_images.py (needs OPENROUTER_API_KEY)"
