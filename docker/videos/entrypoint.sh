#!/bin/sh
# Run the command, then give what it wrote under /out to whoever owns /out.
#
# The container runs as root, so on a Linux host the videos would otherwise
# be root's. (Docker Desktop maps ownership itself; there this does nothing.)
set -u

"$@"
status=$?
owner=$(stat -c %u:%g /out 2>/dev/null) || exit "$status"
if [ "$owner" != "0:0" ]; then
    chown -R "$owner" /out
fi
exit "$status"
