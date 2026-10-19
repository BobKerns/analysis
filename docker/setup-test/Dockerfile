# A clean Ubuntu for exercising scripts/setup.sh from scratch.
#
# Deliberately leaves out uv and any Python newer than the distribution's:
# installing those is the setup script's job, so the container must start
# without them. It carries only what the script assumes is already there
# (git, curl, a POSIX shell, bash for the ripwire installer) and a C
# toolchain for packages that build from source.
#
#   docker build -t analysis-setup-test docker/setup-test
#   docker run --rm -it -v "$PWD":/work analysis-setup-test

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8

# build-essential: gcc, g++, make and the libc headers. pkg-config and the
# -dev libraries cover the C dependencies sdist builds most often look for.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        ca-certificates \
        curl \
        git \
        libffi-dev \
        libssl-dev \
        zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /work
CMD ["bash"]
