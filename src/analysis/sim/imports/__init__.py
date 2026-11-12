"""Bringing outside data into a scenario (issue #527).

```text
outside file -> source -> common form -> mapping -> applier -> scenario CSVs
```

A *source* (``sources.py``) reads one outside layout into a common form
(``forms.py``): holdings, a snapshot of what each outside account holds, or
flows, transactions over a period. The *mapping* (``mapping.py``) says which
of the scenario's names each outside name means; it lives in the data dir's
``imports/`` folder, shared by every scenario. An *applier* (``holdings.py``,
``flows.py``) works out the change to the scenario's CSVs as a plan, which
the CLI shows as a before/after diff and writes through a staged edit.
"""
