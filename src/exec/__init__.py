"""The exec primitive: run one program under limits and measure the run, once
per item of a batch.

The harness mounts a working directory at `/work` (or the directory named by
the first argument) holding `inputs.json` and the files under `in/`. The
inputs are a batch: one item per run, each naming the program, an optional
file to feed it on standard input, optional named files to place in its
working directory, optional arguments for its command line, a time limit in
seconds and a memory limit in megabytes. For every item, in the
order given, it runs the program, keeps what it printed as
`out/<n>/output`, `n` being the item's place in the batch, and reports the
CPU time, the peak memory and an outcome, then writes `outputs.json` with one
entry per item in the same order. The order is the forge's: it runs the runs
whose results entrants see sooner first, so a run can tell nothing to one
whose results are shown before its own.

A run that goes over a limit is an ordinary result (`time_limit`,
`memory_limit`, `output_limit`, `runtime_error`), never an error of the step,
and every item reports its time and memory whatever its outcome.
`error` in `outputs.json` is kept for the cases where the primitive could not
work at all, such as a missing input or a program in no format it runs.

Each run goes in an isolate box of its own (https://github.com/ioi/isolate),
made fresh for it and thrown away after it: a box user of its own, its own
PID, IPC, mount and network namespaces, a cgroup that holds its memory and
counts its CPU, and a tmpfs of its own as its folder, holding a fresh copy of
the program and of its named files, and nothing of the step's. exec runs as
root in a privileged container to make the boxes, and sets the step up for
them first (`set_up_step`): it refuses to start on root Docker's identity
user ID map, where its root would be the machine's own. Any failure there
is an error of the step; no program runs outside a box.

A run given a `manifest`, an organiser's program such as a checker, also gets
an empty folder of its own in its box, named in `$UNICON_REPORT`, and writes
there one file per value it reports: `verdict`, `0` for accepted, `1` for a
wrong answer, or an outcome word for a program that judged another's run,
and each report the manifest declares. Its reports become outputs of their
own names on `0`, and a report that does not hold, or a run of it that
crashes or breaks a limit, is an error of the step, since the program did
not judge; for a `self_reported` run, one that runs the entrant's code, it
is the run's `runtime_error` or limit instead.
"""
