#!/usr/bin/env python3
"""
Deploy processed videos to assets.wheelofheaven.io CDN

Copies processed video files and poster images to the appropriate
category directories in the assets repository.

Usage:
    python deploy_to_cdn.py [--dry-run] [--verbose]
"""

import argparse
import logging
import os
import shutil
import sys
from pathlib import Path

import yaml


# Paths
SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
PROCESSED_DIR = PROJECT_DIR / "processed"
MANIFEST_PATH = PROJECT_DIR / "manifest.yaml"

# CDN repository path (relative to wheelofheaven directory)
CDN_BASE = PROJECT_DIR.parent / "assets.wheelofheaven.io" / "videos"

# Valid CDN categories
VALID_CATEGORIES = [
    "hero",
    "wiki",
    "timeline",
    "library",
    "backgrounds",
    "brand",
]


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure logging."""
    logger = logging.getLogger("deploy")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(ch)
    return logger


def load_manifest() -> dict:
    """Load manifest.yaml."""
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"Manifest not found: {MANIFEST_PATH}")

    with open(MANIFEST_PATH, "r") as f:
        return yaml.safe_load(f)


def deploy_video(
    config: dict,
    dry_run: bool = False,
    logger: logging.Logger = None
) -> bool:
    """Deploy a single video to the CDN."""

    filename = config.get("filename")
    if not filename:
        logger.error("Missing 'filename' in config")
        return False

    if not config.get("enabled", True):
        logger.debug(f"Skipping (disabled): {filename}")
        return True

    # Get output name and category
    output_name = config.get("output_name", Path(filename).stem)
    category = config.get("category", "hero")

    if category not in VALID_CATEGORIES:
        logger.warning(f"Unknown category '{category}', using 'hero'")
        category = "hero"

    # Source files
    video_file = PROCESSED_DIR / f"{output_name}.webm"
    poster_file = PROCESSED_DIR / f"{output_name}-poster.avif"

    # Destination directory
    dest_dir = CDN_BASE / category

    # Check source exists
    if not video_file.exists():
        logger.error(f"Processed video not found: {video_file}")
        logger.error("Run 'mise run process' first")
        return False

    logger.info(f"Deploying: {output_name}")
    logger.info(f"  Category: {category}")
    logger.info(f"  Destination: {dest_dir}")

    if dry_run:
        logger.info(f"  [DRY RUN] Would copy: {video_file.name}")
        if poster_file.exists():
            logger.info(f"  [DRY RUN] Would copy: {poster_file.name}")
        return True

    # Create destination directory
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Copy video
    dest_video = dest_dir / video_file.name
    shutil.copy2(video_file, dest_video)
    video_size = dest_video.stat().st_size / (1024 * 1024)
    logger.info(f"  Copied: {video_file.name} ({video_size:.2f} MB)")

    # Copy poster if exists
    if poster_file.exists():
        dest_poster = dest_dir / poster_file.name
        shutil.copy2(poster_file, dest_poster)
        poster_size = dest_poster.stat().st_size / 1024
        logger.info(f"  Copied: {poster_file.name} ({poster_size:.1f} KB)")

    return True


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Deploy videos to assets.wheelofheaven.io"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without copying"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging"
    )

    args = parser.parse_args()
    logger = setup_logging(args.verbose)

    logger.info("=" * 50)
    logger.info("Video CDN Deployment")
    logger.info("=" * 50)

    # Check CDN directory exists
    if not CDN_BASE.parent.exists():
        logger.error(f"CDN repository not found: {CDN_BASE.parent}")
        logger.error("Clone assets.wheelofheaven.io first")
        sys.exit(1)

    # Load manifest
    try:
        manifest = load_manifest()
    except Exception as e:
        logger.error(f"Failed to load manifest: {e}")
        sys.exit(1)

    videos = manifest.get("videos", [])
    logger.info(f"Found {len(videos)} video(s) in manifest")

    if args.dry_run:
        logger.info("[DRY RUN MODE - No files will be copied]")

    # Deploy each video
    success = 0
    failed = 0

    for config in videos:
        if deploy_video(config, args.dry_run, logger):
            success += 1
        else:
            failed += 1

    logger.info("=" * 50)
    logger.info(f"Deployed: {success} success, {failed} failed")

    if not args.dry_run and success > 0:
        logger.info("")
        logger.info("Next steps:")
        logger.info(f"  cd {CDN_BASE.parent}")
        logger.info("  git add videos/")
        logger.info('  git commit -m "Add processed videos"')
        logger.info("  git push")

    logger.info("=" * 50)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
