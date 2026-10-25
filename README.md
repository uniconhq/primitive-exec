# primitive-exec

The `unicon/exec` primitive, at the forge `unicon/exec@v1`: it runs programs
under limits. A batch item that gives `program` runs that one program,
feeding it a file on standard input if given and any arguments on its
command line, and reports what the program printed, the CPU time and memory
it used, and an outcome. A batch item that gives `programs` runs up to 8
named programs at once, each in a box of its own under limits of its own,
linked by pipes, and reports how each ended. It is what runs code in the
simpler core's workflows: an entrant's program in `unicon/classic@v3`, an
organiser's checker or the diff script after it, and the entrant and judge
of `unicon/interactive@v1`. It replaces `unicon/sandbox-run@v2`, which stays
for the workflow versions that use it.

This repo holds the image, `ghcr.io/uniconhq/primitive-exec`, built from the
`Dockerfile`, with [isolate](https://github.com/ioi/isolate) 2.7 built into
it unpatched and our box launcher, `src/box-launch.c`, beside it; exec
itself, the Python program the image runs, `src/exec.py`, which runs every
program in an isolate box of its own; and `primitive.yaml`, the declaration the
forge compiler reads to type-check a workflow that uses the primitive. exec
speaks the primitive contract, `primitive.schema.json` version 6, published
by the [runner](https://github.com/uniconhq/runner).

## Several programs

The `programs` form. A batch item gives `programs`, one to eight named
programs, and optional `links`, each `"<from> -> <to>"`. The programs of
one item run at once, each in its own box; items still run one at a time,
in the order given. The primitive judges nothing: a later step reads
`facts` and decides the run.

Each program takes `program`, `time_limit` and `memory_limit`, and
optionally `files`, `args`, `manifest` and `stops_others`. A name is a
lowercase word of at most 64 characters, and not `wall_clock`. Each
program's facts are outputs named `<name>.time_ms`, `<name>.memory_kb`,
`<name>.ended`, and optionally `<name>.signal`, `<name>.ended_at` and
`<name>.stopped_by`. A program given a manifest also reports as the one
form's program does, under `<name>.<report>` when its verdict is `0`.
`facts` is `out/<n>/facts.json`, every program's facts by name. `outcome`
is `accepted` when every program was started and measured.

A link is a pipe made before any box starts. A program's first link out is
its standard output and its first link in its standard input; without one,
standard input is `/dev/null` and standard output is read and thrown away,
up to 32 MB. Each further link is a named file, `<other>` in its working
directory, opened for it.

## One program

The `one` form. The primitive takes a batch (`batch: true`): one container runs many runs of
one step, so a hundred runs cost one container start rather than a hundred.
Each item of the batch is keyed by its run, an id of letters, digits, `_` and
`-` that only the forge knows the meaning of, and `outputs.json` answers in
the same order under the same runs. **Items run one at a time, in the order
the batch gives them, and never in another.** The forge relies on it: it
puts the runs whose results entrants see sooner first, since a run can tell
the runs after it a little (see "What a run can still learn"). Each item has
these inputs and outputs.

| | Name | Type | What it is |
|---|---|---|---|
| Input | `program` | file, `runs: true` | The program from compile, in one of the formats below |
| Input | `stdin` | file, optional, `runs: false` | What the program reads on standard input; with none, standard input is empty |
| Input | `files` | named files, optional, `runs: false` | Files placed read-only in the program's working directory under their names, such as `input` or `answer` |
| Input | `loads` | named files, optional, `runs: true` | Code the program loads, placed as `files` are, such as the entrant's module a test harness imports |
| Input | `manifest` | file, optional, `runs: false` | The reports the program declares, its `program.yaml` as JSON, when it is one that reports, such as a checker (see "A program that reports") |
| Input | `self_reported` | boolean, optional | True when the program given a manifest runs the entrant's code, so what it does is the run's own outcome, never a fault |
| Input | `args` | text, optional | Appended to the program's command line, split on spaces |
| Input | `time_limit` | number | Seconds of CPU time the run may use |
| Input | `memory_limit` | number | Megabytes of memory the run may use |
| Output | `output` | file | `out/<n>/output`, `n` the item's place in the batch from 1, what the program printed on standard output |
| Output | `time_ms` | number, `better: lower`, `at_least: 0` | CPU time used, user plus system, in milliseconds; on `time_limit` at least the limit |
| Output | `memory_kb` | number, `better: lower`, `at_least: 0` | Peak memory of the run's box, all it was charged for, its files and the file pages it read among them (below), in kilobytes |
| Output | `label`, `message`, `staff_message` | text, optional | From a program given a manifest: a finer name for the outcome, a message shown with the run, and one for staff alone |
| Output | `extras` | folder, optional | Every other file a program given a manifest reported, shown with the run and never a column |
| Output | each report the manifest declares | number or text | From a program given a manifest, on `accepted` only (`outputs_from: manifest`) |
| Output | `outcome` | outcome | `accepted`, `wrong_answer` (from a manifest's verdict alone), `time_limit`, `memory_limit`, `output_limit` or `runtime_error` (see "Limits and outcomes"); `primitive.yaml` says when each is given under `outcomes`, for the workflow editor |

The output goes under the item's place in the batch, not its run id.

**`files` and `loads`** are copied into the run's working directory under
their names, mode 0444, before the run's clock starts, so the program opens
`answer` as a plain relative path, and lists them and nothing else there. A
name is letters, digits, `_`, `.` and `-`, not starting with a `.`, so never
a path. One name in both is an `error`: each is one file. Each run gets
copies of its own, in its box, which go with it, and exec reads nothing back
from them, so a run that makes a copy writable again, which as their owner
it may, changes only what it reads itself. `loads` differs from `files` only in
`runs: true`: the forge knows the entrant's code is there. A Python program
imports from its working directory once it puts it on `sys.path`, since it
runs with `-I`.

A named file may be **cached**, `{"file": "cache/<item>/<name>"}`: a task
file the step wrapper fetched once into the machine's cache and linked into
an item folder of the step's, which `/work/cache` links to (the job
contract, `runner/schemas/job.schema.json`). It is not copied: the run's
item folder is bound into the box read-only and not executable as
`/cache`, and the file's name in the working directory is a link to
`/cache/<name>`, so a large file costs the box no memory. A run's cached
files, a cached program or `stdin` among them, are all in one item folder
of its own that holds nothing else, or the batch is an `error`, since the
whole folder is in its box; one straight in it, and no other path under
`cache/`, is a cached file. A run reads
`/proc/self/mountinfo`, which names the folder's path on the machine: the
cache volume's, with the org's volume name, in Docker's data folder, such as
`/docker/volumes/<volume>/_data/items/step-<random>/<item>` on Docker
Desktop, the step and item folders' names random.

**`args`** is split on spaces: a run of spaces separates two arguments, and
spaces at either end separate nothing. There is no quoting, so no argument
holds a space. Absent or empty, the program gets no arguments. The arguments
come after the program on the command line: `program <args>`, `python3 -I -B
program <args>`, `java ... -jar program <args>`. An argument no command line
can carry, one holding a NUL character or longer than the kernel takes, is
the run failing to start, a `runtime_error`.

## A program that reports

A run given a `manifest` is an organiser's program, such as a checker or
the diff script, that judges a run and reports values, as `SIMPLER-CORE.md`
13.1 says. It gets an empty folder of its own in its box, `/box/report`,
outside its working directory, named in `UNICON_REPORT`, which it may write
and no other run can reach, and writes there one plain text file per value:

```bash
echo 0                       > $UNICON_REPORT/verdict
echo 1                       > $UNICON_REPORT/score
echo "extra space on line 3" > $UNICON_REPORT/message
```

| File | Required | Becomes |
|---|---|---|
| `verdict` | yes | `0`: the outcome is `accepted` and the reports count; `1`: `wrong_answer`; `time_limit`, `memory_limit`, `output_limit` or `runtime_error`: that outcome |
| `label`, `message`, `staff_message` | no | the text outputs of those names, on any verdict |
| a report the manifest declares | on `0`, unless `optional` | an output of its name on `0`; on any other verdict a file in `extras`, shown and not counted |
| any other name | no | a file in `extras` |

**An outcome word** as the verdict is for a program that judged another
program's run, not its own output: `unicon/interactive`'s verdict script
reads the facts exec's programs form gives of the entrant and the judge, and writes
`time_limit` when the entrant ran out of time. Under a word, as under `1`,
the reports are shown and not counted. From a `self_reported` run a word is
its `runtime_error`, so the entrant's code never chooses its own outcome.

**The manifest** is the program's `program.yaml` in its JSON form, which
the forge writes from the YAML it read and checked at the save, so exec has
no YAML parser of its own to differ from the forge's:

```json
{"reports": {"score": {"type": "number", "better": "higher", "at_least": 0, "at_most": 1},
             "feedback": {"type": "text", "optional": true}}}
```

`type` is `number` when left out; a number may have `better`, `at_least`
and `at_most`, a text none of them; either may be `optional`. A report is
named as an output is, at most 64 characters, and never `verdict`, `label`,
`message`, `staff_message`, `extras` or one of exec's own outputs. exec
reads which way is better from the manifest only to have the forge carry
it; it checks the bounds.

**The checks,** on every run. A value is read whole, at most 64 KB; a
number is a decimal as JSON writes one, `1e-05` among them, with spaces and
newlines around it, finite as a double and inside its bounds, compared as
the decimal written; a text is UTF-8 without a NUL, its trailing newlines
left off. The report does not hold when `verdict` is missing or is not
`0`, `1` or one of the four words; under `0`, a declared report that is not optional is missing,
or a number is not a finite decimal (`nan`, `1e999`) or is outside its
bounds; or, on any verdict, any file in the folder is not UTF-8 text
without a NUL, since every file is shown as text, is over 64 KB, or is not
a plain file under a plain name, links included, or there are more than
64 files. A symbolic link the run leaves there is kept, as exec gives
isolate `--special-files` so the links to cached files stay, and so is a
report that does not hold, whether or not the run had a cached file; exec
reads nothing through it, and the box holds nothing of another run's to
link to.

**Faults.** A report that does not hold, or a program given a manifest that
crashes, exits non-zero or breaks a limit, has not judged: the step is an
`error`, which the forge retries once and then leaves for staff. A
`self_reported` run is the exception: it runs the entrant's code, as a test
harness that imports the entrant's module does, so a report that does not
hold, or one whose verdict is an outcome word, is its `runtime_error`, and
a crash or a limit its own outcome, with no report, as for any run. Its
report is only as honest as the entrant's code: that code runs in the same
process, with `UNICON_REPORT` in reach, and can write a verdict of its own.
A task that needs a verdict the entrant cannot write gives the judging to a
program the entrant's code does not run in, such as the judge exec's programs form
runs beside it.

The folder is in the run's box, so it is fresh for each run and goes with
the box: nothing one run reports is there for the next. A program given no
manifest gets no folder and no `UNICON_REPORT`.

## What it promises

The task's save does not check these; the scoring rules rely on them.

- **The program cannot fault the run.** Everything the program can do is an
  outcome of its own item: going over the time, memory or output limit is
  `time_limit`, `memory_limit` or `output_limit`, and crashing, exiting
  non-zero or failing to start is `runtime_error`. It cannot fill `/work`,
  which its box does not hold: it writes there only its standard output,
  which exec reads through a pipe and cuts at 32 MB. Nor can it stop or
  starve exec, which runs outside its box and holds it to limits inside the
  container's own. The other runs still run.
  None of it is ever `error`, a container killed at its limit or an output
  over the harness's caps.
- **Every item reports its time and memory, whatever its outcome.**
  `output`, `time_ms`, `memory_kb` and `outcome` are written for every item.
  `time_ms` is the CPU time, and on `time_limit` at least the time limit in
  milliseconds, rounded up, whether the CPU limit or the wall clock stopped
  the run: a run that sleeps or waits until the wall clock stops it reports
  the limit, never the few milliseconds of CPU it used. `memory_kb` is its
  peak memory, 0 for a run that could not start.
- **The runs go in the order given.** Item 1 first, then item 2, and so on,
  one at a time.
- **Each item is kept apart from the others.** Every run is in a fresh
  isolate box of its own, running a copy of the program of its own in an
  empty directory, and no run can read or change another run's files or
  find a process, a System V object or a key another left (below). What
  still carries from one run to the next is listed under "What a run can
  still learn".
- **`args` is not `secret`.** It lands on the program's command line, where
  the program reads it, so a task cannot wire a secret into it.

`outputs.json` carries `error` instead, and nothing else, only when the
primitive could not work at all: `inputs.json` is missing, is not JSON or
is for another contract version, or holds `NaN` or `Infinity`; a file is
missing or lies outside `in/`; a limit is not a positive number, or a time
limit under a millisecond or a memory limit under a kilobyte, which isolate
would read as none, or `args` is not text; the `stdin` file is owned by one
of the step's box users; a named file's name is not a plain name, or one
name is in both `files` and `loads`; a manifest is not one, or
`self_reported` is set without one or is not true or false; a program given
a manifest did not judge, unless it is `self_reported` (see "A program that
reports"); an item's run is not a run id, or two items are for the same
run; the program is in none of the three formats; the interpreter a Python
or Java program needs could not be started; exec could not start where it
runs (see "Where it runs"): the container has root Docker's identity map,
exec is not root, `UNICON_BOX_USERS` is missing or out of bounds,
`UNICON_STEP_MEMORY_MB` is missing or not a whole number, the
container's hard limit on the stack or on open files is too low for
isolate, or the step could not be set up for its boxes; or isolate could
not make, run or clean up a box. The harness turns an error into a
`system_error`, so no entrant is graded by a broken step. A native program
the kernel refuses to run as built is the entrant's program failing, a
`runtime_error`.

## How one run goes

For each item, in order:

1. A fresh box. If the box's folder is still a mount point, left by a step
   whose clean-up did not run, it is unmounted first. `isolate --cg --init`
   makes the box, and a tmpfs of its own is mounted over its folder,
   `nosuid` and `nodev`, as large as the memory limit plus what exec copies
   in, holding at most 100,000 files and folders.
2. The program is copied into the box as `/box/program`, and each of
   `files` and `loads` into `/box/run`, the run's working directory,
   read-only, before the run's clock starts. A program given a manifest also
   gets an empty `/box/report`.
3. `isolate --cg --run` runs the copy as the box's user, in PID, IPC, mount
   and network namespaces of the box's own, under its limits (below),
   through the box launcher (see "The box launcher"). The
   box sees `/box`, the image's `/usr`, `/bin` and `/lib` directories and,
   of `/etc`, only Java's `/etc/java-21-openjdk`, all read-only but `/box`,
   its own `/proc`, a `/tmp` and a `/dev/shm` bound from the empty
   `/box/tmp` and `/box/shm` on the box's tmpfs, and a `/dev` of four
   devices. The environment holds only
   `PATH`, `HOME` and `TMPDIR`, both `/box/run`, `LANG=C.UTF-8`, and for a
   program given a manifest `UNICON_REPORT=/box/report`. The `stdin` file,
   or `/dev/null` without one, is opened by exec and handed to the box as
   standard input. A `stdin` file under `/work/in` is not in the box; a
   cached one is, under its random name, in the item folder bound at
   `/cache`. exec opens it through a bind mount it removes at once, so the
   path the box reads for it, through `/proc/self/fd/0`, is `/`, not its
   name. A box could open
   it again through `/proc/self/fd/0` as far as the file's own mode allows,
   so exec first takes write away from its group and others, and refuses a
   file one of the step's box users owns. Standard output is
   a pipe exec reads into `out/<n>/output`, and standard error goes nowhere.
4. exec reads isolate's meta file, and whether the box's tmpfs has a file or
   folder left to give. A program given a manifest has its report read,
   while the tmpfs is still mounted.
5. The tmpfs is unmounted on every way out of the item, errors included,
   which frees all the run wrote there, however deep; then `isolate --cg
   --cleanup` removes the box and its cgroup. Every process the run started
   ended with it: when the box's first process ends, isolate kills the
   rest.

Everything a run prints is kept, up to the output limit, even when it failed.

## Limits and outcomes

isolate holds every limit but the output's, on the box as a whole.

| Limit | How it is held |
|---|---|
| CPU time: `time_limit` seconds | `--time`: the CPU time of every process in the box, added up by its cgroup, and the box killed once over. The time reported is that total, so work hidden in a child that is never waited for still counts |
| Wall-clock time: twice `time_limit` plus one second | `--wall-time`. It stops a run that sleeps or waits without using the CPU; the margin keeps a busy machine from turning a fast run into a time limit |
| Memory: `memory_limit` megabytes | `--cg-mem`: the box's own memory cgroup, with no swap, which the kernel holds on every page it charges there and kills the run at. The memory reported is the cgroup's peak. Java also gets `-Xmx` at the limit |
| Files: the box's tmpfs | As large as the memory limit plus the copies, and 100,000 files and folders, its `/tmp` and `/dev/shm` among them, since both are folders on it. Every page a run writes there is charged to its memory, so writing past the memory limit is `memory_limit`. A run that makes files past the cap gets `ENOSPC`, and exec, finding the tmpfs with none left after the run, gives `memory_limit` too |
| Output: 32 MB | exec reads the pipe up to 32 MB and one byte more, then closes it, so output costs the box no memory. A run that prints on dies of `SIGPIPE`; the output kept is cut to 32 MB |
| Processes: 128 | `--processes`, `RLIMIT_NPROC` for the box's user: every process and thread of the run, a fork bomb's among them |
| Stack | `--stack` at the memory limit, so deep recursion is bounded by memory rather than the default 8 MB |

The outcome is decided in this order: `memory_limit` if the kernel killed
the run for its box's memory, which isolate also reads as a signal, or the
run left its tmpfs no file or folder to give; `time_limit` if the CPU time
or the wall-clock time went over; `output_limit` if the output went over,
before the signal, since a run that printed on died of `SIGPIPE` when exec
stopped reading; `memory_limit` if a Java program exited with code 3, which
is how `-XX:+ExitOnOutOfMemoryError` ends a JVM that ran out of heap; then
`runtime_error` for any other non-zero exit or signal, or a program that
could not start; otherwise `accepted`.

**What `memory_kb` counts.** Everything the kernel charged to the box's
cgroup, at its peak:

- the pages the run's processes touched, so an array that is declared but
  never used does not count;
- the kernel's own memory for them, such as their page tables, about 100 KB
  a process;
- every file the run writes in its box, `/tmp` or `/dev/shm`, all on its
  tmpfs, and its pipe and socket buffers;
- and the pages of every file the run reads that were not in the page cache
  already: its standard input, or a library its interpreter loads.

The last is new with isolate, and the one that surprises. A program that
only reads a 200 MB `stdin` file, holding little, is charged for the pages
of the file it reads in, up to its limit, where the kernel drops the oldest
rather than kill it. Measured on Docker Desktop at a 64 MB limit, it
reported 65,332 KB with the file not yet cached and 11,068 KB once it was,
against 9,552 KB for the same program reading nothing. So a run that reads
a file no one has read since the machine dropped it reports more memory
than one after it, by up to its limit, and reading never ends a run at its
memory limit. The program and the named files are read by exec, which
copies them, so they are never charged to the box. A static C++ program
that does nothing reports 256 to 512 KB, and Python's `print(1)` about
3.8 MB. The box launcher's own share is about 0.1 to 0.2 MB of that, and
no time a run reports.

The figure is the box's cgroup's own peak, not each process's: it counts
every process together, and starts from nothing for each item, since each
item gets a fresh box. A box run twice would read the first run's peak.

## Keeping runs apart

Every run is in a box made for it alone and thrown away after it, so none
of the files, processes, System V objects or keys a run leaves is there for
the next one:

- **Files.** The box's folder is a tmpfs mounted for the run and unmounted
  after it, so all the run wrote there goes at once, and the next run's box
  starts empty. Its `/tmp` and `/dev/shm` are folders on that tmpfs. Of the
  step's own files a box sees none: not `/work`, so not another run's input
  or output or `inputs.json`; its input arrives already open on standard
  input, and no run can write it for the next. The rest it sees, the
  image's system directories and Java's configuration, is read-only. Not
  the container's `/etc`, so not its host name, its address or its name
  server.
- **The program.** Each run gets a copy of the program of its own in its
  box. The run's user owns the copy while it runs, so it may set a mode, a
  time or an extended attribute on it; the copy goes with the box, so none
  of it reaches the next run.
- **Processes.** Each box has a PID namespace of its own: a run sees only
  its own processes in `/proc`, and cannot trace or signal exec or isolate.
  When the box's first process ends, isolate kills the rest. The box's user
  may have at most 128 processes and threads at once, so a fork bomb stops
  there. That count is per user, so it holds as meant only where the box
  users are the step's alone (see "Where it runs").
- **System V objects and POSIX message queues.** Each box has an IPC
  namespace of its own, which goes with it.
- **Keys.** The kernel's keyrings are not namespaced: a key a run added to
  its user's keyring would be there for every later run of that user, its
  description listed in `/proc/keys`. isolate's filter refuses `keyctl`,
  and the box launcher's refuses `add_key`, `request_key` and `keyctl`,
  all with `ENOSYS`.
- **Watches.** A cached file is the same inode in every box that binds it,
  across steps and gradings. isolate's filter refuses `flock`, the file
  locks and `F_NOTIFY` on it, and the box launcher's refuses
  `inotify_init`, `inotify_init1` and `fanotify_init`, with `ENOSYS`, or a
  watch would see another grading open the file. Its link count and change
  time still say how many steps hold it at once.
- **The network.** Each box has a network namespace of its own, with no
  device but loopback, so a run reaches no network even in a container that
  has one, as a Woodpecker step does: a connection out fails with
  `ENETUNREACH`, and a name lookup fails. A Unix socket, which the Java and
  Python runtimes use, still works. isolate's filter refuses `io_uring`
  too, whose operations are calls no filter sees.
- **Devices.** A privileged container's `/dev` holds the machine's devices,
  its disks among them, and isolate's default rules would bind it into the
  box. exec gives every box a `/dev` of its own instead: null, zero, random
  and urandom, bound from the container's, the `fd` and standard stream
  links, and an empty `shm`, where each box's own `/box/shm` is bound. A box
  cannot even name a disk.
- **Privileges.** The run is the box's user, with no capabilities, under
  `no_new_privs`, which isolate and the box launcher set, so no set-user-ID
  program it starts gains any. It keeps the full bounding set isolate
  leaves it, which `no_new_privs` makes worth nothing. The launcher cannot
  drop it: that needs `CAP_SETPCAP`, which the box's user has not.
- **Memory pressure.** A box's memory is its own cgroup's, so a run at its
  limit is killed there, never exec, and exec refuses a batch whose runs'
  limits and copies the container could not hold.

## The box launcher

isolate runs every program through `/usr/local/bin/box-launch`, built from
`src/box-launch.c` in the image's isolate stage and linked statically:
`isolate --run -- /usr/local/bin/box-launch <program> <arguments...>`.
Every box sees it under the `/usr` isolate binds in read-only. It sets
`no_new_privs`, adds a seccomp filter of ours to isolate's, and then runs
the program with `execve`, its arguments, `argv[0]` among them, and its
environment as they were, so the program keeps the launcher's process, its
standard streams and its box, and isolate measures it as before.

The filter allows every call but `add_key`, `request_key`, `keyctl`,
`inotify_init`, `inotify_init1` and `fanotify_init`, which it refuses with
`ENOSYS`, as isolate refuses `keyctl` and `flock`: their kernel objects
are not a box's own (see "Keeping runs apart"). On x86_64 it covers the
32-bit x86 calls too, as isolate's filter does, and a call of any other
architecture, x32 among them, kills the process. A filter cannot be taken
off: a run cannot clear `no_new_privs`, and a filter it adds is one more,
which every process it starts has too.

isolate's own filter cannot do this: its configuration turns its fixed
rules on and off and adds none. The image used to patch isolate for these
calls; the launcher keeps isolate as it is released. When the launcher
cannot set the filter up, or the program cannot start, it says so on the
box's standard error and exits 127, as isolate does when a program cannot
start, and the program does not run. A run's box sends its standard error
nowhere, so that alone would end every run as the program's
`runtime_error`. So each step first runs `box-launch --self-test` once in
a box: it sets the filter up, checks that `inotify_init1` is refused with
`ENOSYS`, and exits 0. When it does not, the step ends in an error, with
what the launcher said, and no program runs.

## What a run can still learn

Of the runs before it, a run can learn none of their files and none of
their processes, System V objects or keys. Its box has no `/sys`, so not the
container's CPU counter, and a PID namespace of its own, so not how many
processes the runs before it started from the IDs its own get. Some numbers
still carry:

- **The clock:** how long the runs before it took, against a time it can
  read, such as its standard input's, a file the harness wrote before the
  batch began.
- **The path its standard input was opened at,** which a box reads through
  `/proc/self/fd/0`: a `stdin` file's path under `/work/in` could say
  which test it is. exec opens the file through a mount that is gone
  before the run starts, so the path a run reads is `/`. The names the
  step gives the files under `/work/in` and in the machine's cache are
  kept random as well (#509), so a path that leaks some other way tells
  nothing either.
- **What the machine counts:** `/proc` in a box is its own for processes,
  but the machine-wide counters in it are not. `/proc/stat` counts every
  process the machine started since it booted, so a run reads how many the
  runs before it started, mixed with every other process on the machine;
  `/proc/loadavg` and `/proc/meminfo` say how busy the machine is.

Each is a number a run before it can move, so a run can pass a later one a
few bits, the machine's counters only through the noise of everything else
it runs. No cleanup closes the clock, which carries between containers too,
so the forge orders the batch: a run can tell only the runs after it, and
none comes before one whose results entrants see sooner.

## Where it runs

exec runs as root in a privileged container, which it needs to make its
boxes, and only where that root is a user namespace's, as under rootless
Docker: the shape picked for every step that runs code, provisionally
(#509). At its start, before any item, it:

1. refuses root Docker's identity map, where a privileged container is the
   machine's own root: the first line of `/proc/self/uid_map` exactly `0 0
   4294967295`. It catches root Docker, not every map whose root is the
   machine's. It also refuses where it cannot read the map, or is not root.
   A development stack, whose laptop has root Docker alone, sets
   `UNICON_DEV_ROOT_DOCKER=1` on every step, and exec then runs under root
   Docker, saying so on standard error. A grading machine never sets it;
2. raises its own soft limits on the stack, to 256 MB, and on open files,
   to 101,024, which isolate and so its boxes inherit. After a run isolate
   gives the box's tree back to root folder by folder, holding one open and
   one stack frame for each level, and a run may nest 100,000 folders deep.
   The box's own limits are isolate's. Raising a hard limit needs the
   machine's root, so exec refuses to start where the container's hard
   limit on either is lower; Docker's default leaves both high enough;
3. makes a cgroup `step`, moves the container's processes into `main`
   under it and makes `isolate` beside that, under which each box gets its
   cgroup, with the CPU and memory controllers on, and caps `step`, with no
   swap, at `UNICON_STEP_MEMORY_MB`, the memory the forge's planner gave
   the step. So steps side by side in one slot of a machine stay within
   what the planner gave each;
4. makes `/run/boxdev`, the boxes' `/dev`;
5. writes isolate's configuration, `/usr/local/etc/isolate`, with the step's
   box users.

Any failure is an `error` of the step, and no program runs outside a box.

**The box users** are `UNICON_BOX_USERS`, `<first>-<last>`, both included,
within 10,000 to 65,000. Whoever starts the step sets it, from the step's
job. They must be users no other step that can run at the same time on the
machine has, and no container's user, since the process limit and the
keyrings are per user. exec runs one box at a time and so uses the first;
it takes a range so that the `programs` form, which runs boxes side by
side, is given its users the same way. Without it, or with a
range out of bounds, exec does not start.

**The step's memory** is `UNICON_STEP_MEMORY_MB`, a whole number of MB,
which whoever starts the step sets from the step's job, as it does the box
users: what the forge's planner gave the step, its largest run's memory
limit and the files copied in beside it, with the 256 MB `primitive.yaml`
adds for the step wrapper, exec and isolate, so it holds the cleanup of a
tree as deep as a box's inodes go. exec does not work the figure out
again: the planner's is the one rule. Without it, exec does not start.

**The machine** needs cgroup v2 with the CPU and memory controllers, and
the container a cgroup namespace of its own, Docker's default on cgroup v2.
isolate's filter does not refuse `bpf`, and a privileged container has no
seccomp profile, so whether a run may make a BPF map is the machine's
`kernel.unprivileged_bpf_disabled`: Ubuntu's default, 2, refuses it, and
Docker Desktop's VM, 0, allows it. The worker's self-test on a grading
machine (feature 12) should check it.

**Not yet run on a real machine.** All of this ran on Docker Desktop
(aarch64, Linux 7.0, cgroup v2, no AppArmor), which is root Docker, with
the tests' `tests/userns.py` giving exec a user namespace to start in. Not
yet run: rootless Docker under Ubuntu 24.04's AppArmor, x86_64, and the
image tests in CI, on GitHub's root Docker. For x86_64 the box launcher
was only cross-compiled and its filter printed: both x86_64 and 32-bit
x86 refuse the six calls, and x32 is killed. No run has made a 32-bit
call.

## The container's limits

`limits` in `primitive.yaml` are per run: 2 s of time and CPU, 256 MB of
memory, 160 processes, 64 MB of output and no GPUs, with no network
(`network: false`). The 160 processes are the box's 128 and room for exec
and isolate. `limits_from` raises them from each run's own limits, so the
box's own limit is the one a run meets, and they are raised for every run,
since every run has a `time_limit`. Time and CPU go to `2 ×
time_limit + 3` seconds, what one run can take at most:

- the wall-clock limit, `2 × time_limit + 1` seconds, which a run that
  sleeps or waits reaches;
- and a little for the rest: making the box, copying in, cleaning up, and
  exec's own start and the step's set-up, once per batch. A run that sleeps
  took 3.03 s against a 3 s wall clock, a run that does nothing 5 to 10 ms
  in all, and a batch 0.2 s to start, measured in the image on Docker
  Desktop with one CPU.

Memory goes to `memory_limit + 256` MB: room for the step wrapper, exec
and isolate beside the box. The copies of the program and the named files
in the box's tmpfs are charged to exec, not the box, and the forge's
planner adds them on top, each at the most it may be, and gives the step
the sum (`UNICON_STEP_MEMORY_MB`). A run whose memory limit and copies,
with 112 MB for the step wrapper, exec and isolate, are more than the
step's memory, or the container's (its cgroup's `memory.max`) when that is
less, is an error of the step, said before any run of the batch starts.
The limit would otherwise kill first, the step with no outputs at all, or
a run, which isolate reads as the run's own `memory_limit`. The `forge`
repo's compiler adds up time and CPU over the runs of the batch when it
writes the plan. A box's processes inherit the container's `RLIMIT_CPU`,
when it sets one, which is above the run's own limit, so isolate's limit
is the one that stops a run.

## The program format

`program` is one file, in one of three formats. compile writes them, and
exec tells them apart by their content: an ELF header, or a zip
holding a `__main__.py` at its root, or a zip holding a jar manifest.

| Language | The program | How exec runs it |
|---|---|---|
| `c`, `cpp` | A statically linked ELF executable | Its copy marked executable and run directly |
| `python` | A Python zip application behind the line `#!/usr/bin/env python3`: the source folder under `source/` with its layout, and a `__main__.py` at the root that runs the entry | `python3 -I -B program` on its copy, on the image's Python 3.14 |
| `java` | A runnable jar: the compiled classes and a manifest naming the main class | `java -Xmx<memory_limit>m -Xss64m -XX:+UseSerialGC -XX:-UsePerfData -XX:+ExitOnOutOfMemoryError -Djava.io.tmpdir=. -jar program`, on the image's OpenJDK 21, started by its real path, since `/usr/bin/java` goes through `/etc/alternatives` |

Static linking means a native program needs nothing from the image it runs
in. A Python or Java program needs the interpreter it was checked against, so
both images build on the runner's `primitive-base`, the same pinned
`python:3.14-slim`, and
install the same Debian OpenJDK 21 packages: the JDK in compile, the runtime
in exec. A change to any of the three formats, or to either image's
Python or Java, is made in both repos together.

## Layout

```
Dockerfile                  the image: isolate and the box launcher built in a stage of their own, primitive-base, OpenJDK 21 runtime
src/exec.py                 exec, installed as /usr/local/bin/exec, the image's command
src/box-launch.c            the box launcher, installed as /usr/local/bin/box-launch: the keyring and watch calls refused
primitive.yaml              the declaration, without the image line
tests/                      unit tests, and image tests that run it on Docker
tests/Dockerfile            the test container: the image with the tests' tools
tests/userns.py             starts a command in a user namespace, for exec on root Docker
```

`primitive.yaml` has no `image` line here: bootstrap writes the image by
digest, from the release manifest, into the version it creates at the forge.
Its `title`, its `description`s and the `outcome` port's `outcomes`, each
outcome exec can report with a line saying when, are what the
workflow editor shows; a test keeps `outcomes` to exactly the outcomes
`judge` gives.

## Running it locally

Python 3.14 with [uv](https://docs.astral.sh/uv/), and Docker for the image
tests.

```
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run python ../runner/scripts/check_declaration.py ../runner/schemas/primitive.schema.json .
uv run pytest
```

The declaration check is the runner's, so it runs from a `runner` checkout
beside this one, at the release named in `.github/workflows/ci.yaml`.

`uv run pytest` runs what it can where it runs. The image tests (marked
`image`) build the image, or use `PRIMITIVE_IMAGE`, and run it as an exec
step is run: privileged, with the declared memory, pids and CPU limits,
one CPU, the tests' box users and the network Docker gives it, as a
Woodpecker step has one, on a Docker volume holding the working directory.
On root Docker, as Docker Desktop and a CI machine are, they start exec
through `tests/userns.py`, which gives it a user namespace whose map is not
the identity, so that exec's own checks run as they would under rootless
Docker. Without Docker they are skipped.

The in-process tests, `tests/test_exec.py`, import exec, which needs Linux,
so elsewhere only the image tests are collected. Those that run programs
need root and isolate too, so on a host they are skipped, and they run in
the test container: `tests/Dockerfile` builds it on the image with the
tests' tools, and the image test
`test_the_in_process_tests_pass_in_the_test_container` runs
`tests/test_exec.py` in it, privileged, where none is skipped.

The image builds on the runner's `primitive-base` (`ARG BASE_IMAGE`), whose
step wrapper is the entrypoint and runs `exec` as the image's command in
a grading step, and runs it in its own place anywhere else. The Dockerfile
names the runner release's image; the image tests build one from a `runner`
checkout beside this one, or take `BASE_IMAGE`. By hand:

```
docker build -t primitive-base:test -f ../runner/images/primitive-base/Dockerfile ../runner
docker build -t primitive-exec:test --build-arg BASE_IMAGE=primitive-base:test .
docker build -t primitive-exec:test-container --build-arg IMAGE=primitive-exec:test \
  -f tests/Dockerfile .
docker run --rm --privileged --env=UNICON_BOX_USERS=60000-60009 \
  --env=UNICON_STEP_MEMORY_MB=1024 -v "$PWD:/src:ro" \
  primitive-exec:test-container python3 tests/userns.py pytest -p no:cacheprovider tests/test_exec.py
```

The image tests build their C++, Java and Python programs with the compile
primitive's own image, so they are also the check that the two primitives
agree on the program format. That image is `COMPILE_IMAGE`, or built from a
`primitive-compile` checkout beside this one; without either, those tests
are skipped. CI builds it from `primitive-compile`'s `main`. The tests ask
it in contract version 6, as compile@v3 speaks it, and ask a compile image
that speaks version 5, compile@v2, again in its shape.
The checks on every `inputs.json` and `outputs.json` the tests see read the
schema from `PRIMITIVE_SCHEMA`, or from a `runner` checkout beside this one.

`.github/workflows/ci.yaml` and `release.yaml` call the workflows every
primitive shares, `primitive-ci.yaml` and `primitive-release.yaml` in the
[runner](https://github.com/uniconhq/runner) repo, at the runner release this
primitive is built against, and name the same release as `runner-ref`. They
check this repo out beside the runner at that release, so the checks read
its `primitive.schema.json` and run its `scripts/check_declaration.py`. CI
runs the checks and unit tests in one job, where the tests that run programs
are skipped, and builds the image and runs the image tests in another, the
test container's run of them among them. That job has not yet run on
GitHub's machines with exec on isolate. Moving to a new runner release is a change to the
two `uses:` lines and `runner-ref` together.

## Releasing

Push a tag `v1.0.0` on `main`. The tag's major is the version at the forge:
`v1.0.0` is a release of `unicon/exec@v1`, so a change to the ports is a new
major. The shared release workflow refuses a tag whose commit is not
on `main` and a tag that differs from the version in `pyproject.toml`. It
runs the same checks as CI, pushes the image as
`ghcr.io/uniconhq/primitive-exec:v1.0.0`, and creates a GitHub release
with `images.json`, which names the image by digest in the same shape as the
runner's, and `primitive.yaml` attached, and the digest in the notes.
`deploy/images.json` pins that digest, and bootstrap writes it into the
forge's copy of `primitive.yaml`.

The first push creates the package on the organisation as **private**.
Grading machines pull it anonymously, so someone has to open the package on
the organisation's Packages page once, set its visibility to public, and add
this repo under Manage Actions access so later releases can keep pushing to
it.
