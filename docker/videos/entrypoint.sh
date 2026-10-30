#!/bin/sh
# Run the command, then give what it wrote under /out to whoever owns /out.
#
# The container runs as root, so on a Linux host the videos would otherwise
# be root's. (Docker Desktop maps ownership itself; there this does nothing.)
# docker.sh runs the container with --init: as PID 1 this shell would
# ignore `docker stop` and Ctrl-C without a terminal, so the build ran on
# until Docker killed it. A stopped run skips the chown below.
set -u

"$@"
status=$?
owner=$(stat -c %u:%g /out 2>/dev/null) || exit "$status"
if [ "$owner" != "0:0" ]; then
    # Only what this run made: a full chown -R would walk every cached clip.
    find /out \( ! -uid "${owner%:*}" -o ! -gid "${owner#*:}" \) -exec chown -h "$owner" {} +
fi
exit "$status"
