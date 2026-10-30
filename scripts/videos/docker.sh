#!/usr/bin/env bash
# Build videos in a container that has everything they need (#382).
#
#   scripts/videos/docker.sh build VIDEO [--lang L] [--silent] [--rerecord]
#   scripts/videos/docker.sh check [VIDEO ...]
#   scripts/videos/docker.sh shell VIDEO
#
# VIDEO is a folder in docs/videos, by name (01-intro) or path
# (docs/videos/01-intro). Videos land in out/videos, or $ANALYSIS_VIDEO_OUT.
#
# Each run (re)builds the analysis-videos image first, from this checkout;
# that's quick when nothing changed. docs/videos is mounted read-only, so
# script edits need no rebuild; a code change rebuilds the code's layers.
#
# Each video has its own data repo, in the named volume analysis-video-VIDEO
# mounted at /data. A tape whose script names no demo data (data = ...)
# starts from a copy of it. `shell VIDEO` opens bash in the container with
# the volume mounted and ANALYSIS_DATA_DIR=/data/sim, to create the repo
# (`sim init /data --no-builder`) or change it with the CLI.
# `docker volume rm analysis-video-VIDEO` discards it.
set -eu

cd "$(dirname "$0")/../.."

image=analysis-videos

usage() {
    sed -n '2,20s/^# \{0,1\}//p' "$0"
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
        ;;
    *) echo "docker.sh: unknown command $command (build, check or shell)" >&2; exit 2 ;;
esac

# A first build takes several minutes; show it. Later ones are near no-ops.
if docker image inspect "$image" >/dev/null 2>&1; then
    docker build -q -t "$image" -f docker/videos/Dockerfile . >/dev/null
else
    echo "Building the $image image (first time: several minutes)..." >&2
    docker build -t "$image" -f docker/videos/Dockerfile .
fi

out=${ANALYSIS_VIDEO_OUT:-out/videos}
mkdir -p "$out"
out=$(cd "$out" && pwd)

docker_run=(docker run --rm -i)
if [ -t 0 ] && [ -t 1 ]; then
    docker_run+=(-t)
fi
docker_run+=(-v "$(pwd)/docs/videos:/repo/docs/videos:ro" -v "$out:/out")
if [ -n "$name" ]; then
    docker_run+=(-v "analysis-video-$name:/data")
fi

case $command in
    build)
        exec "${docker_run[@]}" "$image" python -m analysis.videos build "docs/videos/$name" --out /out "$@"
        ;;
    check)
        folders=()
        for v in "$@"; do
            folders+=("docs/videos/$(video_name "$v")")
        done
        exec "${docker_run[@]}" "$image" python -m analysis.videos check ${folders[@]+"${folders[@]}"}
        ;;
    shell)
        exec "${docker_run[@]}" -w /data "$image" bash "$@"
        ;;
esac
