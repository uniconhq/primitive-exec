/*
 * box-launch, the first program of every box: isolate runs it as the box's
 * user, and it runs the program it is given, with its arguments and
 * environment as they are, in its place:
 *
 *     box-launch <program> [arguments...]
 *     box-launch --self-test
 *
 * First it adds a seccomp filter of ours to isolate's, refusing with
 * ENOSYS the calls that reach kernel objects no box has its own of:
 * add_key, request_key and keyctl, since the keyrings are per user, not
 * per box, and a key one box adds is listed in /proc/keys for the next box
 * of its user; and inotify_init, inotify_init1 and fanotify_init, since a
 * watch on a file boxes share, a cached file, sees another grading open
 * it. isolate's own filter refuses keyctl, flock and F_NOTIFY, and its
 * configuration can only turn its rules off, never add one.
 *
 * The filter allows every other call. It covers the native calls and, on
 * x86_64, the 32-bit x86 ones, as isolate's does; a call of any other
 * architecture kills the process. no_new_privs is set first, which a
 * filter needs and isolate has set already. A filter cannot be taken off,
 * and every process the program starts has it.
 *
 * The capability bounding set stays as isolate leaves it: dropping from it
 * needs CAP_SETPCAP, which the box's user has not, and under no_new_privs
 * no capability can be gained anyway.
 *
 * When it cannot set the filter up, or the program cannot start, it says
 * so on standard error and exits 127, as isolate does when a program
 * cannot start. A run's box sends its standard error nowhere, so no one
 * reads that, and the run ends as the program's runtime error. So exec
 * starts `box-launch --self-test` once in a box when a step
 * starts: it sets the filter up, checks that inotify_init1 is refused
 * with ENOSYS, and exits 0, or says what failed and exits 127, which
 * fails the step before any program runs.
 */
#include <errno.h>
#include <seccomp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/inotify.h>
#include <sys/prctl.h>
#include <unistd.h>

extern char **environ;

static void
die(const char *what, int error)
{
  if (error)
    fprintf(stderr, "box-launch: %s: %s\n", what, strerror(error));
  else
    fprintf(stderr, "box-launch: %s failed\n", what);
  exit(127);
}

int
main(int argc, char **argv)
{
  static const int refused[] = {
    SCMP_SYS(add_key),
    SCMP_SYS(request_key),
    SCMP_SYS(keyctl),
    SCMP_SYS(inotify_init),
    SCMP_SYS(inotify_init1),
    SCMP_SYS(fanotify_init),
  };
  int err;
  int self_test = argc == 2 && strcmp(argv[1], "--self-test") == 0;

  if (argc < 2)
    {
      fprintf(stderr, "usage: box-launch <program> [arguments...]\n");
      return 127;
    }
  if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0)
    die("prctl(PR_SET_NO_NEW_PRIVS)", errno);

  scmp_filter_ctx ctx = seccomp_init(SCMP_ACT_ALLOW);
  if (!ctx)
    die("seccomp_init", 0);
  err = seccomp_attr_set(ctx, SCMP_FLTATR_ACT_BADARCH, SCMP_ACT_KILL_PROCESS);
  if (err < 0)
    die("seccomp_attr_set", -err);
  if (seccomp_arch_native() == SCMP_ARCH_X86_64)
    {
      err = seccomp_arch_add(ctx, SCMP_ARCH_X86);
      if (err < 0)
        die("seccomp_arch_add(x86)", -err);
    }
  /*
   * A call one architecture lacks, as aarch64 lacks inotify_init, is left
   * out for that architecture, where no program can make it.
   */
  for (unsigned int i = 0; i < sizeof(refused) / sizeof(refused[0]); i++)
    {
      err = seccomp_rule_add(ctx, SCMP_ACT_ERRNO(ENOSYS), refused[i], 0);
      if (err < 0)
        die("seccomp_rule_add", -err);
    }
  err = seccomp_load(ctx);
  if (err < 0)
    die("seccomp_load", -err);
  seccomp_release(ctx);

  if (self_test)
    {
      if (inotify_init1(0) >= 0)
        {
          fprintf(stderr, "box-launch: the filter let inotify_init1 through\n");
          return 127;
        }
      if (errno != ENOSYS)
        die("inotify_init1 under the filter, not refused with ENOSYS", errno);
      return 0;
    }
  execve(argv[1], argv + 1, environ);
  fprintf(stderr, "execve(\"%s\"): %s\n", argv[1], strerror(errno));
  return 127;
}
