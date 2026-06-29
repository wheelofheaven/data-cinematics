#!/usr/bin/env bash
# Re-render all TBWTT chapter videos for the Latin-script languages (the
# caption font is Latin-only; CJK/Cyrillic/Hebrew need a font swap first).
# Uses the new normalized higher-stability audio + 4x smooth Ken Burns +
# intro cards + score bed. Each chapter ~7 min.
set -uo pipefail
cd "$(dirname "$0")"
PY=/Users/zara/Development/github.com/wheelofheaven/data-images/venv/bin/python
BOOK=the-book-which-tells-the-truth
for lang in en fr de es; do
  echo "############ render $BOOK / $lang ############"
  "$PY" compose_video.py --book "$BOOK" --lang "$lang" 2>&1
  echo "==== rendered $lang ===="
done
echo "ALL_VIDEOS_DONE"
ls -lh out/$BOOK/*.mp4 | grep -vE "preview" | wc -l
