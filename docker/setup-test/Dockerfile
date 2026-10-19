# A clean Ubuntu for exercising scripts/setup.sh from scratch.
#
# Deliberately leaves out uv and any Python newer than the distribution's:
# installing those is the setup script's job, so the container must start
# without them. It carries the distribution's Python, what the script assumes
# is already there (git, curl, a POSIX shell, bash for the ripwire installer),
# ssh for cloning, and a C toolchain for packages that build from source.
#
# /work is a named volume holding the clones (see entrypoint.sh); the host's
# ~/.ssh is mounted read-only for the clones' ssh URLs:
#
#   docker build -t analysis-setup-test docker/setup-test
#   docker run --rm -it \
#       -v analysis-test:/work \
#       -v ~/.ssh:/root/.ssh-host:ro \
#       analysis-setup-test
#
# The container doesn't inherit the host's DNS search domains, so if a clone
# URL's host is a short name the host completes from one, pass that domain
# with --dns-search. CODE_REPO_URL and DATA_REPO_URL (-e) override the URLs.
#
# Inside, run ./scripts/setup.sh and give it /work/finance-data as the data
# repo. `docker volume rm analysis-test` starts the next run from scratch.

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8

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
