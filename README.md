# data-videos

Video asset pipeline for Wheel of Heaven. Converts source videos to optimized WebM (AV1) format for web delivery.

## Directory Structure

```
data-videos/
├── raw/                    # Source video files
├── processed/              # Converted WebM files and poster images
├── backup/                 # Original file backups
├── scripts/                # Processing scripts
├── manifest.yaml           # Video processing configuration
└── mise.toml               # Task runner configuration
```

## Prerequisites

- **FFmpeg** with SVT-AV1 encoder: `brew install ffmpeg`
- **Python 3.8+**
- **mise** task runner: `brew install mise`

Verify FFmpeg has AV1 support:
```bash
ffmpeg -encoders | grep svtav1
```

## Quick Start

```bash
# 1. Set up Python environment
mise run setup

# 2. Add videos to raw/ and update manifest.yaml

# 3. Preview processing
mise run dry-run

# 4. Process videos
mise run process

# 5. Deploy to CDN
mise run deploy
```

## Configuration

Edit `manifest.yaml` to configure video processing:

```yaml
videos:
  - filename: "source-video.mp4"    # Source file in raw/
    category: "hero"                 # CDN category
    quality: "medium"                # low, medium, high
    crop_watermark: true             # Crop bottom portion
    crop_height: 50                  # Pixels to crop
    output_name: "video-name"        # Output filename
    enabled: true                    # Enable/disable
```

### Quality Presets

| Preset | CRF | Use Case |
|--------|-----|----------|
| low    | 45  | Previews, bandwidth-constrained |
| medium | 32  | Default, good balance |
| high   | 25  | Hero content, important videos |

### CDN Categories

- `hero` - Homepage and landing page videos
- `wiki` - Article illustrations
- `timeline` - Historical age visualizations
- `library` - Book-related videos
- `backgrounds` - Background/ambient videos
- `brand` - Branding and promotional

## Available Tasks

```bash
mise run setup           # Set up Python environment
mise run process         # Process all enabled videos
mise run dry-run         # Preview without changes
mise run process-force   # Reprocess all videos
mise run deploy          # Deploy to CDN
mise run deploy-dry-run  # Preview deployment
mise run full-pipeline   # Process + deploy
mise run clean           # Remove processed files
mise run check           # Verify dependencies
mise run info            # Show processed file info
mise run count           # Count raw vs processed
```

## Output Format

- **Video**: WebM container with AV1 codec (libsvtav1)
- **Audio**: Opus codec at 128kbps
- **Poster**: AVIF thumbnail (first frame)

## Deployment

Processed videos are deployed to:
```
assets.wheelofheaven.io/videos/{category}/
├── video-name.webm
└── video-name-poster.avif
```

After `mise run deploy`, commit and push the changes:
```bash
cd ../assets.wheelofheaven.io
git add videos/
git commit -m "Add processed videos"
git push
```

## License

CC0-1.0
