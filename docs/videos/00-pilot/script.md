+++
series = "Planning with the simulator"
episode = "Pilot"
title = "Will the money last?"
subtitle = "Running Alice's retirement scenario and reading its net worth"
data = "prototype_data"
+++

The pilot for #373: the shared title card, one terminal scene, and an
ending card. It exists to prove the pipeline, so it's short.

## Scene `title`: Title card

```slide
template = "title"
```

Meet Alice and Bill. They're planning their retirement, and they want to
know one thing: will the money last?

## Scene `networth`: Run the scenario and read the net worth

```tape
Hide
Type "sim switch retire_alice > /dev/null 2>&1 && clear"
Enter
Show
Sleep 600ms
Type "sim run"
Sleep 400ms
Enter
Sleep 2.5s
Type "sim networth"
Sleep 400ms
Enter
Sleep 9s
```

Alice's retirement plan is a scenario called retire alice. The sim run
command steps through it a month at a time. Then sim networth shows the
result: one row per year, in today's dollars, after the taxes they would owe
to spend it.

> Production note: "retire alice" is spelled with a space so the voice
> doesn't say "underscore".

## Scene `end`: Ending card

```slide
template = "card"
heading = "Next: building a scenario"
body = "Accounts, chains and the calendar, from an empty folder."
speaker = "alice"
```

Next time, we'll build a scenario like this one from scratch.
