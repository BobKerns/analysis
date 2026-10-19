# A clean Ubuntu for exercising scripts/setup.sh from scratch.
#
# Deliberately leaves out uv and any Python newer than the distribution's:
# installing those is the setup script's job, so the container must start
# without them. It carries the distribution's Python, what the script assumes
# is already there (git, curl, a POSIX shell, bash for the ripwire installer),
# ssh for cloning, and a C toolchain for packages that build from source.
#
# Start it with scripts/start-container.sh, which builds the image and runs
# it with a named volume at /work holding the clones (see entrypoint.sh) and
# the host's ~/.ssh mounted read-only for the clones' ssh URLs. Inside, run
# ./scripts/setup.sh (or start with --setup) and give it /work/finance-data
# as the data repo; `start-container.sh --fresh` starts the next run from
# scratch.

FROM ubuntu:24.04

# UV_LINK_MODE=copy: the uv cache and /work are on different filesystems,
# so uv can't hard-link from one to the other (and warns each time it tries).
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    UV_LINK_MODE=copy

# build-essential: gcc, g++, make and the libc headers. python3-dev adds
# Python.h for the distribution's Python; pkg-config and the other -dev
# libraries cover the C dependencies sdist builds most often look for.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        ca-certificates \
        curl \
        git \
        openssh-client \
        python3 \
        python3-dev \
        python3-venv \
        libffi-dev \
        libssl-dev \
        zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /work
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bash"]
