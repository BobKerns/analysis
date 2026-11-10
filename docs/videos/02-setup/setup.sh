# Prepares what video 2 shows, before any scene records (docs/videos/README.md,
# "Setting up the demo"). It runs inside a fresh, committed copy of the
# prototype data, with `sim` on PATH, which this video doesn't use: it starts
# from nothing, as a new user does. So this makes the data repo the install
# would have made, in a home folder of its own beside the prototype copy, for
# the scenes after `sim init` to start from. The init scene removes it first
# and makes it again on camera.

export HOME="$(dirname "$ANALYSIS_DATA_DIR")/home"
mkdir -p "$HOME"
unset ANALYSIS_DATA_DIR
# The same answers the init scene types. ANALYSIS_NO_BUILDER (set for every
# recording) keeps it from linking this checkout or turning on builds.
sim init ~/retirement/data --id alice --display "Alice Example" --birthdate 1962-04-15 \
    --sex f --filing-status MFJ
