#!/usr/bin/env python3
"""Body word count for a tear sheet: text before the Sources heading,
ignoring table pipes, table separator rows, markdown link URLs and bare URLs.

Usage: python wordcount.py tear-sheet.md
"""
import re
import sys

text = open(sys.argv[1]).read()
m = re.search(r"^#{1,3} +(\d+\.? *)?Sources\b(?! of)", text, re.M | re.I)
body = text[: m.start()] if m else text
lines = [line for line in body.splitlines() if not re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", line)]  # drop |---|---| rows
body = "\n".join(lines)
body = re.sub(r"\]\([^)]*\)", "]", body)          # link targets
body = re.sub(r"https?://\S+", "", body)          # bare URLs
body = re.sub(r"[|#*_`>]", " ", body)              # markdown punctuation
words = [w for w in body.split() if re.search(r"\w", w)]
print(len(words))
