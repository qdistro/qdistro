/* syscost.c — self-timed syscall-cost probe for the tier-3s bench.
 * Prints one line: getpid_ms_per_100k=X openclose_ms_per_20k=Y forkexec_ms_per_1k=Z
 * Built on the bench host, run on the host / inside tier-2 / inside runsc.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static long long now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000000LL + ts.tv_nsec / 1000;
}

int main(void)
{
    long long t0, t1;
    double a, b, c;
    int i, fd;

    t0 = now_us();
    for (i = 0; i < 100000; i++)
        (void)getpid();
    t1 = now_us();
    a = (double)(t1 - t0) / 1000.0;

    t0 = now_us();
    for (i = 0; i < 20000; i++) {
        fd = open("/dev/null", O_RDONLY);
        if (fd < 0) { perror("open"); return 1; }
        close(fd);
    }
    t1 = now_us();
    b = (double)(t1 - t0) / 1000.0;

    t0 = now_us();
    for (i = 0; i < 1000; i++) {
        pid_t p = fork();
        if (p < 0) { perror("fork"); return 1; }
        if (p == 0) {
            char *av[] = { "/bin/true", NULL };
            execv("/bin/true", av);
            _exit(127);
        }
        if (waitpid(p, NULL, 0) < 0) { perror("waitpid"); return 1; }
    }
    t1 = now_us();
    c = (double)(t1 - t0) / 1000.0;

    printf("getpid_ms_per_100k=%.3f openclose_ms_per_20k=%.3f forkexec_ms_per_1k=%.3f\n", a, b, c);
    return 0;
}
