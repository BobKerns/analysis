#!/bin/sh
# Block commits if any files in data/, out/, or state/ are staged
if git diff --cached --name-only | grep -E '^(data|out|state)/'; then
  echo "\033[0;31mERROR: Attempt to commit files in data/, out/, or state/. These directories are git-ignored and must not be committed.\033[0m"
  exit 1
fi
