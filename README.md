# primitive-exec

The `unicon/exec` primitive, at the forge `unicon/exec@v1`: it runs one
program under limits, feeding it a file on standard input if given and any
arguments on its command line, and reports what the program printed, the CPU
time and memory it used, and an outcome. It is what runs code in the
simpler core's workflows: an entrant's program in `unicon/classic@v3`, and
an organiser's checker or the diff script after it. It replaces
`unicon/sandbox-run@v2`, which stays for the workflow versions that use it.

This repo holds the image, `ghcr.io/uniconhq/primitive-exec`, built from the
`Dockerfile`; exec itself, the Python program the image runs, `src/exec.py`; the small C program
every program is started through, `src/sandbox-exec.c`; and
`primitive.yaml`, the declaration the forge compiler reads to type-check a
workflow that uses the primitive. exec speaks the primitive contract,
`primitive.schema.json` version 6, published by the
[runner](https://github.com/uniconhq/runner).

## What it takes and returns

The primitive takes a batch (`batch: true`): one container runs many runs of
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
| Output | `memory_kb` | number, `better: lower`, `at_least: 0` | Peak resident memory of the run, all its processes together (below), in kilobytes |
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
copies of its own, removed after it, and exec reads nothing back from them,
so a run that makes a copy writable again, which as its owner it may,
changes only what it reads itself. `loads` differs from `files` only in
`runs: true`: the forge knows the entrant's code is there. A Python program
imports from its working directory once it puts it on `sys.path`, since it
runs with `-I`.

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
13.1 says. It gets an empty folder of its own under `/tmp`, outside its
working directory, named in `UNICON_REPORT`, which it may write and no
other run may, and writes there one plain text file per value:

```bash
echo 0                       > $UNICON_REPORT/verdict
echo 1                       > $UNICON_REPORT/score
echo "extra space on line 3" > $UNICON_REPORT/message
```

| File | Required | Becomes |
|---|---|---|
| `verdict` | yes | `0`: the outcome is `accepted` and the reports count; `1`: `wrong_answer` |
| `label`, `message`, `staff_message` | no | the text outputs of those names, on either verdict |
| a report the manifest declares | on `0`, unless `optional` | an output of its name on `0`; on `1` a file in `extras`, shown and not counted |
| any other name | no | a file in `extras` |

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
`0` or `1`; under `0`, a declared report that is not optional is missing,
or a number is not a finite decimal (`nan`, `1e999`) or is outside its
bounds; or, on either verdict, any file in the folder is not UTF-8 text
without a NUL, since every file is shown as text, is over 64 KB, or is not
a plain file under a plain name, links included, or there are more than
64 files. A hard link into the folder from outside the run's own folders
is refused by Landlock.

**Faults.** A report that does not hold, or a program given a manifest that
crashes, exits non-zero or breaks a limit, has not judged: the step is an
`error`, which the forge retries once and then leaves for staff. A
`self_reported` run is the exception: it runs the entrant's code, as a test
harness that imports the entrant's module does, so a report that does not
hold is its `runtime_error`, and a crash or a limit its own outcome, with no
report, as for any run. Its report is only as honest as the entrant's code:
that code runs in the same process, with `UNICON_REPORT` in reach, and can
write a verdict of its own. A task that needs a verdict the entrant cannot
write gives the judging to a program the entrant's code does not run in,
such as `interact`'s partner.

The folder is fresh for each run and removed after it, so nothing one run
reports is there for the next. A program given no manifest gets no folder
and no `UNICON_REPORT`.

## What it promises

The task's save does not check these; the scoring rules rely on them.

- **The program cannot fault the run.** Everything the program can do is an
  outcome of its own item: going over the time, memory or output limit is
  `time_limit`, `memory_limit` or `output_limit`, and crashing, exiting
  non-zero, failing to start or killing `sandbox-exec`, the process that
  started it, is `runtime_error`. It cannot fill `/work`, where it writes only
  its standard output, cut at 32 MB, nor stop or starve exec, which
  holds it to limits inside the container's own. The other runs still run.
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
- **Each item is kept apart from the others.** Every run is a new process
  in a new, empty directory, running a copy of the program of its own, and
  no run can read or change another run's files or find a System V object
  another left (below). What still carries from one run to the next is
  listed under "What a run can still learn".
- **`args` is not `secret`.** It lands on the program's command line, where
  the program reads it, so a task cannot wire a secret into it.

`outputs.json` carries `error` instead, and nothing else, only when the
primitive could not work at all: `inputs.json` is missing, is not JSON or is
for another contract version, or holds `NaN` or `Infinity`; a file is missing or lies outside `in/`; a
limit is not a positive number, or `args` is not text; a named file's name
is not a plain name, or one name is in both `files` and `loads`; a manifest
is not one, or `self_reported` is set without one or is not true or false;
a program given a manifest did not judge, unless it is `self_reported`
(see "A program that reports"); an item's run is not
a run id, or two items are for the same run; the program is in none of the
three formats; the interpreter a Python or Java program needs could not be
started; the machine's kernel does not offer Landlock ABI 3, or a run could
not be put under its seccomp filter or its Landlock rules; or processes, a
System V object, an attribute or a file a run left behind could not be
stopped or removed. The harness
turns an error into a `system_error`, so no entrant is graded by a broken
step. A native program the kernel refuses to run as built is the entrant's
program failing, a `runtime_error`.

## How one run goes

For each item, in order:

1. The program is copied into a scratch directory under `/work`, a copy for
   this item alone, before its clock starts.
2. A fresh, empty directory is made under `/tmp` and becomes the run's
   working directory, `HOME` and `TMPDIR`. The environment holds only those,
   `PATH` and `LANG=C.UTF-8`, and for a program given a manifest
   `UNICON_REPORT`, a second fresh folder under `/tmp`.
3. Each of `files` and `loads` is copied into that directory, read-only.
   The `stdin` file, or `/dev/null` without one, is opened as standard
   input, `out/<n>/output` as standard output, and standard error goes
   nowhere.
4. The copy is started through `sandbox-exec`, with its arguments and its
   limits already set (below), under the seccomp filter and Landlock, and
   watched until it ends.
5. Every process the run left behind is killed and reaped, every System V
   shared memory segment, message queue and semaphore set it left is
   removed, every `user` extended attribute and access control list under
   `/work` is removed, and the directory, however deep the run built it,
   and the copy are removed. The run could make no POSIX
   message queue. A program given a manifest has its report read, and the
   folder removed.

Everything a run prints is kept, up to the output limit, even when it failed.

## Limits and outcomes

| Limit | How it is held |
|---|---|
| CPU time: `time_limit` seconds | Every process of the run, added up, read every 5 ms from `/proc` and the program killed once over; `RLIMIT_CPU` at the limit rounded up plus a second as a backstop. The time reported is the program's own with its waited-for children, from `wait4`, plus every process it left behind, which exec reaps, so work hidden in a child that is never waited for still counts |
| Wall-clock time: twice `time_limit` plus one second | Checked every 5 ms. It stops a run that sleeps or waits without using the CPU; the margin keeps a busy machine from turning a fast run into a time limit |
| Memory: `memory_limit` megabytes | The run's peak memory, read from `/proc` while it runs and the program killed once over, then checked again against the program's own final peak from `wait4`. It is the highest of three figures: the peak resident memory of any one process, which the kernel keeps; the highest total of all the run's processes seen, with each page two or more of them share divided among them (`Pss` in `/proc/<pid>/smaps_rollup`), so a child forked from the program is charged for the pages it changes and not again for those it shares with its parent; and the most the container held beyond what it held before the run began: everything charged to its cgroup (`memory.current`) but the pages of files on disk, which counts what no process's pages do, files the run keeps in its own directory, which is on the `/tmp` tmpfs, files it deleted and keeps open, memory files (`memfd_create`), and pipe and socket buffers. Only one run goes at a time, so what the container gains is the run's. That figure is read every 5 ms from the container's own cgroup, and on a machine where exec is not alone in a cgroup of its own it is not read and a warning goes to standard error. The total of the processes is read at most every 5 ms too: it walks every page the run holds, about 25 µs a megabyte, so it is read only when the processes together hold more than the peak so far, never for a run of one process, and for at most a fifth of the time. The kernel keeps no peak of the last two figures, so memory held for less than the time between two readings is not caught, and the container's own limit holds it. Java also gets `-Xmx` at the limit |
| Output: 32 MB | Checked every 5 ms, and `RLIMIT_FSIZE` just above it, so no write can go further; an output that went over is cut to 32 MB |
| Stack | `RLIMIT_STACK` set to the memory limit, so deep recursion is bounded by memory rather than the default 8 MB |

The outcome is decided in this order: the limit the run was stopped for, if
it was; `output_limit` if the output went over or the program was killed by
`SIGXFSZ`; `time_limit` if the CPU time or the wall-clock time went over or
the program was killed by `SIGXCPU`; `memory_limit` if the peak memory went
over, a Java program exited with code 3, which is how
`-XX:+ExitOnOutOfMemoryError` ends a JVM that ran out of heap, or the kernel
killed the program for the container's memory, which the container's
`memory.events` counts, having held more than the container allows,
between two readings or where exec could not count it; then
`runtime_error` for any other non-zero exit or signal; otherwise
`accepted`.

A run stopped at a limit has its program killed and `sandbox-exec` woken, in
case the run stopped it with `SIGSTOP` where Landlock does not keep signals
in, so the item ends within a second of the limit.

Memory is resident memory, the pages the program actually touched, so an
array that is declared but never used does not count against it. A file the
run writes in its own directory counts too, since that directory is in
memory, and so does the kernel's own memory for the run's processes, such as
their page tables, about a megabyte for a small program.

**Why `sandbox-exec`.** Linux carries a process's peak resident memory across
`exec`, so a program started straight from the Python program would report at
least the interpreter's own 20 MB. `sandbox-exec` is a static C program of a
few hundred kilobytes: it forks, puts the child under the run's Landlock
rules, runs the program in it, waits for it and reports its exit status, CPU
time and peak memory from `wait4` on a pipe the program does not inherit. A C++ program that does nothing reports about
1.5 MB, which is what it used.

## Keeping runs apart

Every run is a new process in a new, empty directory, and none of the files,
processes or System V objects a run leaves is there for the next one:

- **Leftover processes.** exec is the child subreaper of everything it
  starts, so a process that outlives its run, even one that started its own
  session, becomes exec's child. After every run each such child is
  killed and reaped before the next run starts.
- **System V objects.** The container has an IPC namespace of its own, so
  every shared memory segment, message queue and semaphore set in it is one
  a run made. After every run exec reads `/proc/sysvipc` and removes
  each one; one it cannot remove is an `error` of the step. On a machine
  without `/proc/sysvipc` the kernel has none to leave, and a warning goes
  to standard error.
- **The program.** Each run gets a copy of the program of its own, removed
  after it. A run is exec's user, so it may set an extended
  attribute, a mode or a time on the file it runs, which Landlock does not
  stop; on a copy of its own, none of it reaches the next run. Copying
  costs nothing a run is measured on, and at most about 17 ms a run for a
  32 MB program, the largest compile makes, measured on Docker Desktop.
- **Extended attributes.** A run may set a `user` extended attribute on any
  file or folder it can name and write, which Landlock does not stop: the
  next run's input, `out/`, its own output. On what its user owns it may
  also set an access control list, whose entries name any user ID, and a
  default one on a folder. After every run exec removes every one of
  these under `/work`, so none keeps work for the next run. A run may also
  take exec's access away from what it marked, with `chmod`, so each path
  is given back first; an attribute that still can't be removed is an
  `error` of the step.
- **What a run leaves in `/tmp`.** A run may build folders deeper than any
  path can name in its working directory, fill them and take exec's access
  away from them. exec removes them through open folders, never by path,
  so the next run has as much of `/tmp` as the first; what still can't be
  removed is an `error` of the step. Removing takes several times what
  making took, on no run's clock, so a run that makes more than 100,000
  files and folders in `/tmp` is stopped for its memory, `memory_limit`.
- **POSIX message queues.** A queue outlives the process that made it, and
  the kernel lists none to remove, so a run is given no bytes for one
  (`RLIMIT_MSGQUEUE` at 0) and can make none.
- **What exec needs.** A run may take exec's access away from
  anything it can name and its user owns: the program under `in/`, its own
  copy and the folder that copy is in. exec gives each back before
  it copies the next program, so no run can end the whole batch in an error.
- **Files.** Every run is put under [Landlock](https://docs.kernel.org/userspace-api/landlock.html).
  It may create, change or remove files only inside its own directory, and
  its report folder when it has a manifest, plus write to `/dev/null`. It
  may read only those, its program, the
  system's directories (`/usr`, `/bin`, `/sbin`, the `/lib` directories,
  `/etc`, `/proc`, `/sys`), `/dev/null`, `/dev/zero` and the random devices,
  and for a Python program the interpreter's installation; its input arrives
  already open on standard input. It cannot read or list `/work` or `/tmp`,
  so it cannot see another run's input or output or `inputs.json`, nor
  change the files `outputs.json` is built from, and nothing it writes is
  there for a later run to read. Running a file needs reading it, so a run
  can start its program and the system's programs and nothing else.
- **The program that watches it.** A run cannot trace exec or
  `sandbox-exec`, or open their memory or file descriptors through `/proc`:
  both make themselves not dumpable, and Landlock, which `sandbox-exec`
  enforces on the program alone just before starting it, forbids tracing
  anything outside the run as well. From Landlock ABI 6 a run cannot signal
  either of them. On an older kernel it can kill `sandbox-exec`, its parent;
  it then ends as its own `runtime_error`, judged on what exec
  measured itself. It cannot stop exec: exec is the
  container's first process, which the kernel keeps signals from unless it
  handles them, and it ignores the one Python would handle, SIGINT.
- **The network and io_uring.** `sandbox-exec` puts the run under a
  seccomp filter of its own, after no-new-privileges and before Landlock:
  `socket` and `socketpair` fail with `EPERM` for every family but
  `AF_UNIX`, and `io_uring_setup` fails with `EPERM`, since a ring's
  operations are calls neither the filter nor Landlock sees. `add_key`,
  `request_key` and `keyctl` fail with `EPERM` too: the kernel's keyrings
  are not namespaced, so a key a run linked into its user's keyring would
  be there for every later run of that user, in any container. On x86_64 every
  x32 call is refused too, and a call of another architecture (i386 on
  x86_64, arm32 on aarch64) kills the run. So a run reaches no network and
  no netlink even in a container that has one, as a Woodpecker step may; a
  Unix socket, which the Java and Python runtimes use, still works. The
  filter does not lean on Docker's own profile: the tests show its refusals
  with that profile off.
- **Processes.** `RLIMIT_NPROC` is the container's pids limit, or 128,
  `pids` in `primitive.yaml`, when the container sets none or a higher one,
  as a Woodpecker step does: a fork bomb stops there, and its leftovers are
  reaped after the run. It counts every process and thread of the run's
  user, exec's own among them, so it holds as meant only where that user
  is the grading's alone.
- **Memory pressure.** Every run's processes carry the highest out-of-memory
  score, so when the container reaches its memory limit the kernel kills
  the run rather than exec.

**The kernel it needs.** Landlock at ABI 3 or later: Linux 6.2 or later,
with Landlock among the kernel's security modules (`lsm=`; Docker Desktop's
kernel has it, and `/sys/kernel/security/lsm` on a machine lists it).
Ubuntu 24.04's 6.8 kernel has ABI 4, and Docker Desktop's current one ABI 8.
Below ABI 3 nothing runs: `outputs.json` carries an `error`, which the
harness turns into a `system_error`, never a grade. ABI 1 confines reads and
writes, and ABI 3 adds `truncate` of a file named by path, without which a
run could empty or lengthen a file it cannot even open, such as another
run's output, which it can name. Later
ABIs add to it where the kernel has them: from ABI 2 a run may rename
within its own directory, and from ABI 6 it cannot signal `sandbox-exec`, as
above. Requiring ABI 6 would turn away most machines on a 6.1 to 6.11 kernel
for a gap that costs only the run itself. The worker's self-test on a
grading machine (feature 12) should check the kernel offers Landlock ABI 3
before the machine takes runs.

The container itself is the harness's: no network, a read-only root
filesystem, every capability dropped, `no-new-privileges`, Docker's built-in
seccomp profile and a non-root user; the image's user is 65532. The walls
above hold inside whatever container exec is given. The only
writable places are `/work` and a small tmpfs at `/tmp`. Each run's copy of
the program goes into a scratch directory under `/work`, removed before
exec exits, because `/tmp` may be mounted `noexec`.

## What a run can still learn

Of the runs before it, a run can learn none of their files' contents and
none of their System V objects, but a little more, through what the
container keeps between items:

- **The container's CPU counter** (`cpu.stat` in its cgroup, under `/sys`,
  which the Java runtime reads): how much CPU the runs before it used, to
  the microsecond.
- **The process ID counter:** how many processes the runs before it
  started, from the ID its own processes get.
- **The clock:** how long the runs before it took, against a time it can
  read, such as its input's.
- **Files it can name:** that one exists, and its size, mode and times,
  which Landlock does not confine and which a run can change on a file of
  its own user it can name; and the free space of `/work`. A mode a run
  takes away from a file or folder of that user is given back after it, so
  the next run and the harness still read and write them.

Each is a number a run before it can choose, so a run can pass a later one
a few bits. No cleanup inside one container closes the first three, and
the clock carries between containers too, so the forge orders the batch: a
run can tell only the runs after it, and none comes before one whose
results entrants see sooner.

## The container's limits

`limits` in `primitive.yaml` are per run: 2 s of time and CPU, 256 MB of
memory, 128 processes, 64 MB of output and no GPUs, with no network
(`network: false`). `limits_from` raises them from each run's own limits
so the container never dies before exec's own limit does, and they
are raised for every run, since every run has a `time_limit`. Time and
CPU go to `2 × time_limit + 3` seconds, what one run can take at most:

- the wall-clock limit, `2 × time_limit + 1` seconds, which a run that
  sleeps or waits reaches;
- up to one second more when a run keeps `sandbox-exec` from reporting,
  before its whole process group is killed (see "Limits and outcomes");
- and about a second for the rest: starting the run under Landlock, reaping
  what it left behind and removing its directory, and exec's own
  start, once per batch. A sleeping run took 0.2 to 0.6 s beyond the
  wall-clock limit per run, and a batch 1.5 to 2 s to start, measured in
  the image under the harness's flags with one CPU on a loaded laptop.

Memory goes to `memory_limit + 256` MB (room for exec itself and for
the moment between two memory readings). The `forge` repo's compiler adds up
time and CPU over the runs of the batch when it writes the plan. The
harness holds the CPU limit as `RLIMIT_CPU` on the container's processes,
exec among them, and each run lowers its own to the time limit
rounded up plus a second, which is always below it.

## The program format

`program` is one file, in one of three formats. compile writes them, and
exec tells them apart by their content: an ELF header, or a zip
holding a `__main__.py` at its root, or a zip holding a jar manifest.

| Language | The program | How exec runs it |
|---|---|---|
| `c`, `cpp` | A statically linked ELF executable | Its copy marked executable and run directly |
| `python` | A Python zip application behind the line `#!/usr/bin/env python3`: the source folder under `source/` with its layout, and a `__main__.py` at the root that runs the entry | `python3 -I -B program` on its copy, on the image's Python 3.14 |
| `java` | A runnable jar: the compiled classes and a manifest naming the main class | `java -Xmx<memory_limit>m -Xss64m -XX:+UseSerialGC -XX:-UsePerfData -XX:+ExitOnOutOfMemoryError -Djava.io.tmpdir=. -jar program`, on the image's OpenJDK 21 |

Static linking means a native program needs nothing from the image it runs
in. A Python or Java program needs the interpreter it was checked against, so
both images are built from the same pinned `python:3.14-slim` base and
install the same Debian OpenJDK 21 packages: the JDK in compile, the runtime
in exec. A change to any of the three formats, or to either image's
Python or Java, is made in both repos together.

## Layout

```
Dockerfile                  the image: python:3.14-slim, OpenJDK 21 runtime, sandbox-exec
src/exec.py                 exec, installed as /usr/local/bin/exec, the image's entrypoint
src/sandbox-exec.c          the launcher, built static and installed as /usr/local/bin/sandbox-exec
primitive.yaml              the declaration, without the image line
tests/                      unit tests, and image tests that run it on Docker
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

`uv run pytest` runs everything: the unit tests, which need Linux and gcc
(they build `sandbox-exec` and run Python programs on the interpreter running
the tests), and the image tests (marked `image`), which build the image and
run it under the harness's sandbox flags and the container limits the
declaration gives. Elsewhere only the image tests are collected. Without
Docker the image tests are skipped. They use `PRIMITIVE_IMAGE` instead of
building when it is set. They run the image on
a Docker volume holding the working directory, as the harness gives a step
one: Landlock's rules do not hold on a directory Docker Desktop shares from
Windows or macOS, whose files it cannot tell apart from one open to the next.

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
runs the checks and unit tests in one job, and builds the image and runs the
image tests in another. Moving to a new runner release is a change to the
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
