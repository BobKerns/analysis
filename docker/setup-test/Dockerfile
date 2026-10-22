# A clean Ubuntu for exercising scripts/setup.sh from scratch, as close to a
# fresh WSL Ubuntu as a container gets: the image's stock ubuntu user (uid
# 1000, with Ubuntu's default dotfiles and sudo), a login shell, and of the
# packages a fresh install has, the ones setup.sh relies on: git, curl, ssh
# and the distribution's Python. A C toolchain, uv, Node.js and a newer Python
# are left out on purpose: installing those is the setup script's job.
#
# Unlike WSL, sudo asks for no password, so --setup can run unattended.
#
# Start it with scripts/start-container.sh, which builds the image and runs
# it with a named volume at /work holding the clones (see entrypoint.sh) and
# the host's ~/.ssh mounted read-only for the clones' ssh URLs. Inside, run
# ./scripts/setup.sh (or start with --setup) and give it /work/finance-data
# as the data repo if DATA_REPO_URL cloned one, or a blank answer for
# template data; `start-container.sh --fresh` starts the next run from
# scratch.

FROM ubuntu:24.04

# UV_LINK_MODE=copy: the uv cache and /work are on different filesystems,
# so uv can't hard-link from one to the other (and warns each time it tries).
ENV LANG=C.UTF-8 \
    UV_LINK_MODE=copy

# /work is created here, owned by ubuntu, so a new volume mounted on it is
# too.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        openssh-client \
        python3 \
        sudo \
 && rm -rf /var/lib/apt/lists/* \
 && echo 'ubuntu ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/ubuntu \
 && chmod 440 /etc/sudoers.d/ubuntu \
 && mkdir /work \
 && chown ubuntu:ubuntu /work

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER ubuntu
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# A login shell, as WSL starts.
CMD ["bash", "-l"]
