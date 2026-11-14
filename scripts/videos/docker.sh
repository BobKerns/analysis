#!/usr/bin/env bash
# Build videos in a container that has everything they need (#382).
#
#   scripts/videos/docker.sh build VIDEO [--lang L] [--silent] [--rerecord]
#   scripts/videos/docker.sh check [VIDEO ...]
#   scripts/videos/docker.sh shell VIDEO
#   scripts/videos/docker.sh image
#
# VIDEO is a folder in docs/videos, by name (01-intro) or path
# (docs/videos/01-intro). Videos land in out/videos, or $ANALYSIS_VIDEO_OUT.
#
# The checkout (MOUNTS) is mounted read-only, and the project's
# dependencies are synced from it into the volume analysis-videos-env at
# each start (#612), so neither a code nor a dependency change needs a new
# image. The analysis-videos image is built, from docker/videos alone, only
# when it's missing or a file there changed; `image` rebuilds it from
# scratch, with the newest base image and system packages.
#
# Each video has its own data repo, in the named volume analysis-video-VIDEO
# mounted at /data. A tape whose script names no demo data (data = ...)
# starts from a copy of it. `shell VIDEO` opens bash in the container with
# the volume mounted and ANALYSIS_DATA_DIR=/data/sim, to create the repo
# (`sim init /data --no-builder`) or change it with the CLI.
# `docker volume rm analysis-video-VIDEO` discards it.
set -eu

# Relative to where it was run from, not the repo root.
out=${ANALYSIS_VIDEO_OUT:-}
if [ -n "$out" ]; then
    mkdir -p "$out"
    out=$(cd "$out" && pwd)
fi

cd "$(dirname "$0")/../.."

image=analysis-videos
# The image is built from docker/videos alone; a change to a file there
# rebuilds it.
context=docker/videos
label=analysis.videos.inputs
env_volume=analysis-videos-env
# The checkout, mounted read-only over the image's /repo: piece by piece,
# so the host's .venv (perhaps for another OS) stays out.
MOUNTS=(src pyproject.toml uv.lock README.md .python-version templates historical_data
        prototype_data notebooks scripts/videos docs/videos)

usage() {
    sed -n '2,23s/^# \{0,1\}//p' "$0"
}

video_name() {
    name=$(basename "$1")
    if [ ! -f "docs/videos/$name/script.md" ]; then
        echo "docker.sh: no video $name (no docs/videos/$name/script.md)" >&2
        exit 2
    fi
    printf '%s\n' "$name"
}

[ $# -gt 0 ] || { usage >&2; exit 2; }
command=$1
shift

case $command in
    -h|--help) usage; exit 0 ;;
    build|shell)
        [ $# -gt 0 ] || { echo "docker.sh: $command needs a VIDEO" >&2; exit 2; }
        name=$(video_name "$1")
        shift
        ;;
    check)
        name=
        # Named before the build, so a typo doesn't wait for it.
        folders=()
        for v in "$@"; do
            folders+=("docs/videos/$(video_name "$v")")
        done
        ;;
    image) [ $# -eq 0 ] || { echo "docker.sh: image takes no arguments" >&2; exit 2; } ;;
    *) echo "docker.sh: unknown command $command (build, check, shell or image)" >&2; exit 2 ;;
esac

# One digest of the build context, labelled on the image. git hash-object
# works on macOS and Linux alike (sha256sum isn't on macOS).
inputs=$(find "$context" -type f ! -name .DS_Store | LC_ALL=C sort | while read -r f; do
    printf '%s %s\n' "$(git hash-object "$f")" "${f#"$context"/}"
done | git hash-object --stdin)

# BuildKit, for TARGETARCH. A build takes several minutes; show it.
export DOCKER_BUILDKIT=1
build_image() {
    docker build "$@" --label "$label=$inputs" -t "$image" "$context"
}
if [ "$command" = image ]; then
    echo "Rebuilding the $image image from scratch (several minutes)..." >&2
    build_image --pull --no-cache
    exit
fi
if ! built=$(docker image inspect -f "{{index .Config.Labels \"$label\"}}" "$image" 2>/dev/null); then
    echo "Building the $image image (first time: several minutes)..." >&2
    build_image
elif [ "$built" != "$inputs" ]; then
    # (An image from before #612 has no label, so it's rebuilt once too.)
    echo "docker/videos changed; rebuilding the $image image..." >&2
    build_image
fi

if [ -z "$out" ]; then
    mkdir -p out/videos
    out=$(pwd)/out/videos
fi

docker_run=(docker run --rm --init -i)
if [ -t 0 ] && [ -t 1 ]; then
    docker_run+=(-t)
fi
for m in "${MOUNTS[@]}"; do
    docker_run+=(-v "$(pwd)/$m:/repo/$m:ro")
done
docker_run+=(-v "$env_volume:/opt/env" -v "$out:/out")
if [ -n "$name" ]; then
    docker_run+=(-v "analysis-video-$name:/data")
fi

case $command in
    build)
        exec "${docker_run[@]}" "$image" python -m analysis.videos build "docs/videos/$name" --out /out "$@"
        ;;
    check)
        exec "${docker_run[@]}" "$image" python -m analysis.videos check ${folders[@]+"${folders[@]}"}
        ;;
    shell)
        exec "${docker_run[@]}" -w /data "$image" bash "$@"
        ;;
esac
