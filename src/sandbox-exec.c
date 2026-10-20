/*
 * sandbox-exec: start one program as a child and report what it used.
 *
 *     sandbox-exec REPORT_FD RULESET_FD PROGRAM [ARGUMENT...]
 *
 * exec starts this small static program instead of the program itself,
 * with the run's limits already set, and this program forks and runs the
 * binary. The reason is memory: Linux carries a process's peak resident set
 * across exec(2), so a program started straight from exec's Python would
 * report at least the interpreter's own size. Forked from this program it
 * starts from almost nothing and reports its own peak.
 *
 * RULESET_FD is a Landlock ruleset exec made for the run; without one
 * nothing is started. The child enforces it on itself just before exec, so
 * the binary and everything it starts are confined and this program is not:
 * from Landlock ABI 6 the binary cannot signal it, and from ABI 1 it cannot
 * trace it. This program also makes itself not dumpable, so the binary,
 * which runs as the same user, cannot open its file descriptors, the report
 * pipe among them, through /proc.
 *
 * Before the ruleset, the child installs a seccomp filter on itself, which
 * the binary and everything it starts inherit, and refuses with EPERM:
 *
 *   - socket() and socketpair() for any family but AF_UNIX, so the binary
 *     reaches no network and no netlink, whatever the container was given;
 *   - io_uring_setup(), since a ring's operations are not system calls the
 *     filter or Landlock sees;
 *   - on x86_64, every x32 call, whose numbers would pass the checks above.
 *
 * A call made under another architecture (i386 on x86_64, arm32 on
 * aarch64) kills the process, since its numbers mean other calls.
 * Everything else is allowed. The filter is classic BPF written here, so
 * the static build needs no library.
 *
 * Lines written to REPORT_FD, which the binary does not inherit:
 *
 *     P <pid>                                 the binary's process id
 *     S <errno>                               the filter could not be installed
 *     L <errno>                               the ruleset could not be enforced
 *     E <errno>                               the binary could not be started
 *     R <status> <user_us> <system_us> <maxrss_kb>   how it ended and what it used
 */

#include <errno.h>
#include <fcntl.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef __NR_landlock_restrict_self
#define __NR_landlock_restrict_self 446
#endif
#ifndef __NR_io_uring_setup
#define __NR_io_uring_setup 425
#endif

#if defined(__x86_64__)
#define NATIVE AUDIT_ARCH_X86_64
#elif defined(__aarch64__)
#define NATIVE AUDIT_ARCH_AARCH64
#else
#error "sandbox-exec's filter knows x86_64 and aarch64 only"
#endif

#define REFUSE (SECCOMP_RET_ERRNO | (EPERM & SECCOMP_RET_DATA))

/* The family, socket()'s first argument, is an int: the filter reads its
 * low 32 bits, as the kernel does. */
static struct sock_filter rules[] = {
    BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, NATIVE, 1, 0),
    BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
    BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
#if defined(__x86_64__)
    BPF_JUMP(BPF_JMP | BPF_JGE | BPF_K, 0x40000000, 0, 1),
    BPF_STMT(BPF_RET | BPF_K, REFUSE),
#endif
    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_io_uring_setup, 0, 1),
    BPF_STMT(BPF_RET | BPF_K, REFUSE),
    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_socket, 1, 0),
    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_socketpair, 0, 3),
    BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[0])),
    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AF_UNIX, 1, 0),
    BPF_STMT(BPF_RET | BPF_K, REFUSE),
    BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
};

static int install_filter(void)
{
    struct sock_fprog program = {
        .len = sizeof rules / sizeof rules[0],
        .filter = rules,
    };
    return (int)syscall(__NR_seccomp, SECCOMP_SET_MODE_FILTER, 0, &program);
}

static long long microseconds(struct timeval t)
{
    return (long long)t.tv_sec * 1000000 + t.tv_usec;
}

int main(int argc, char **argv)
{
    if (argc < 4)
        return 2;
    int report = atoi(argv[1]);
    int ruleset = atoi(argv[2]);
    if (fcntl(report, F_SETFD, FD_CLOEXEC) != 0)
        return 2;
    if (ruleset < 0 || fcntl(ruleset, F_SETFD, FD_CLOEXEC) != 0) {
        dprintf(report, "L %d\n", EBADF);
        return 2;
    }
    if (prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0)
        return 2;

    pid_t pid = fork();
    if (pid < 0)
        return 2;
    if (pid == 0) {
        if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) {
            dprintf(report, "L %d\n", errno);
            _exit(127);
        }
        if (install_filter() != 0) {
            dprintf(report, "S %d\n", errno);
            _exit(127);
        }
        if (syscall(__NR_landlock_restrict_self, ruleset, 0) != 0) {
            dprintf(report, "L %d\n", errno);
            _exit(127);
        }
        close(ruleset);
        execv(argv[3], argv + 3);
        dprintf(report, "E %d\n", errno);
        _exit(127);
    }
    close(ruleset);
    dprintf(report, "P %d\n", (int)pid);

    int status;
    struct rusage usage;
    while (wait4(pid, &status, 0, &usage) < 0)
        if (errno != EINTR)
            return 2;
    dprintf(report, "R %d %lld %lld %ld\n", status, microseconds(usage.ru_utime),
            microseconds(usage.ru_stime), usage.ru_maxrss);
    return 0;
}
