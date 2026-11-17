/*
 * cpu-window: router-side CPU and memory sampler for QDX experiments.
 *
 * Usage: cpu-window INTERVAL_MS COUNT
 *   Every INTERVAL_MS milliseconds, COUNT times, it reads every "cpu*" line of
 *   /proc/stat and MemTotal/MemFree/MemAvailable of /proc/meminfo and writes
 *   one record per line to stdout, flushing after each sample. Before the
 *   first sample it writes "ready" to stderr (window.py waits for that line).
 *   SIGINT or SIGTERM stops it after the current sample.
 *
 * Output records (space separated):
 *   <sample> <monotonic_ns> cpu <name> <user> <nice> <system> <idle> <iowait> <irq> <softirq> <steal>
 *   <sample> <monotonic_ns> mem <MemTotal_kB> <MemFree_kB> <MemAvailable_kB>
 *   <sample> <monotonic_ns> late <ns>
 * CPU fields are cumulative jiffies (USER_HZ) exactly as /proc/stat reports
 * them; utilisation is computed by the reader from two consecutive samples.
 * "late" appears only when a sample started more than one interval after its
 * planned time. Samples are planned at absolute times start + n * interval
 * (clock_nanosleep TIMER_ABSTIME), so delays do not accumulate; a late sample
 * is not made up.
 *
 * Exit status: 0 = COUNT samples written; 1 = a /proc file could not be read
 * or a signal stopped it early; 2 = usage error.
 *
 * Build: one static binary (not an OpenWrt package), OpenWrt toolchain inside
 * the project's Docker wrapper. The wrapper mounts only WRT_WORKTREE, so the
 * notes repository is not visible in the container: copy this file into that
 * tree first (e.g. its tmp/). The toolchain directory name depends on the
 * configured target; check it with 'ls staging_dir'. Used on 2026-10-16:
 *   export WRT_WORKTREE=<tree>; cp cpu-window.c "$WRT_WORKTREE/tmp/"
 *   scripts/wrt-docker-build.sh 'STAGING_DIR=$PWD/staging_dir \
 *       staging_dir/toolchain-aarch64_cortex-a53_gcc-14.4.0_musl/bin/aarch64-openwrt-linux-gcc \
 *       -static -O2 -Wall -Wextra -o tmp/cpu-window tmp/cpu-window.c'
 * Copy to a running RAM system (dropbear has no scp/sftp server; use cat) with
 * ssh-run.py, which runs ssh only when the route to the router uses eno2:
 *   ssh-run.py --ssh root@192.168.1.1 192.168.1.115 eno2 --stdin cpu-window \
 *       --command 'cat > /tmp/cpu-window && chmod 0755 /tmp/cpu-window && ls -l /tmp/cpu-window'
 *   then check the router file mode is 0755 before relying on it.
 * Use in a window: the window closes when its runs end (at most --duration),
 * so COUNT * INTERVAL_MS must fit in that time plus 2 * --tail, or window.py
 * stops the sampler and the window fails. With runs lasting all 10 s:
 *   window.py --duration 10 ... --router '/tmp/cpu-window 100 105' --ssh root@192.168.1.1 192.168.1.115 eno2
 */

#define _GNU_SOURCE
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static volatile sig_atomic_t stop_requested;

/* Record that a stop signal arrived. */
static void on_signal(int signum)
{
	(void)signum;
	stop_requested = 1;
}

/* Return CLOCK_MONOTONIC as nanoseconds. */
static long long monotonic_ns(void)
{
	struct timespec now;

	clock_gettime(CLOCK_MONOTONIC, &now);
	return (long long)now.tv_sec * 1000000000LL + now.tv_nsec;
}

/* Write one cpu record per "cpu*" line of /proc/stat; return 0 on success. */
static int write_cpu(long sample, long long now)
{
	char line[512], name[32];
	unsigned long long v[8];
	FILE *stat = fopen("/proc/stat", "r");
	int lines = 0;

	if (!stat)
		return -1;
	while (fgets(line, sizeof(line), stat)) {
		if (strncmp(line, "cpu", 3) != 0)
			continue;
		if (sscanf(line, "%31s %llu %llu %llu %llu %llu %llu %llu %llu", name,
			   &v[0], &v[1], &v[2], &v[3], &v[4], &v[5], &v[6], &v[7]) != 9)
			continue;
		printf("%ld %lld cpu %s %llu %llu %llu %llu %llu %llu %llu %llu\n", sample, now, name,
		       v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7]);
		lines++;
	}
	fclose(stat);
	return lines > 0 ? 0 : -1;
}

/* Write the mem record from /proc/meminfo; return 0 on success. */
static int write_mem(long sample, long long now)
{
	char line[256];
	long long total = -1, free_kb = -1, available = -1;
	FILE *info = fopen("/proc/meminfo", "r");

	if (!info)
		return -1;
	while (fgets(line, sizeof(line), info)) {
		sscanf(line, "MemTotal: %lld kB", &total);
		sscanf(line, "MemFree: %lld kB", &free_kb);
		sscanf(line, "MemAvailable: %lld kB", &available);
	}
	fclose(info);
	if (total < 0 || free_kb < 0 || available < 0)
		return -1;
	printf("%ld %lld mem %lld %lld %lld\n", sample, now, total, free_kb, available);
	return 0;
}

int main(int argc, char **argv)
{
	struct sigaction action;
	struct timespec due;
	long long interval_ns, start, planned, now;
	long count, sample;
	char *end_interval, *end_count;

	if (argc != 3) {
		fprintf(stderr, "usage: cpu-window INTERVAL_MS COUNT\n");
		return 2;
	}
	interval_ns = strtoll(argv[1], &end_interval, 10) * 1000000LL;
	count = strtol(argv[2], &end_count, 10);
	if (*end_interval || *end_count || interval_ns <= 0 || count <= 0) {
		fprintf(stderr, "usage: cpu-window INTERVAL_MS COUNT (both positive integers)\n");
		return 2;
	}
	memset(&action, 0, sizeof(action));
	action.sa_handler = on_signal;
	sigaction(SIGINT, &action, NULL);
	sigaction(SIGTERM, &action, NULL);

	fprintf(stderr, "ready\n");
	fflush(stderr);
	start = monotonic_ns();
	for (sample = 0; sample < count && !stop_requested; sample++) {
		planned = start + sample * interval_ns;
		due.tv_sec = planned / 1000000000LL;
		due.tv_nsec = planned % 1000000000LL;
		while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &due, NULL) == EINTR && !stop_requested)
			;
		if (stop_requested)
			break;
		now = monotonic_ns();
		if (now - planned > interval_ns)
			printf("%ld %lld late %lld\n", sample, now, now - planned);
		if (write_cpu(sample, now) || write_mem(sample, now)) {
			fprintf(stderr, "cannot read /proc/stat or /proc/meminfo: %s\n", strerror(errno));
			return 1;
		}
		fflush(stdout);
	}
	return sample == count ? 0 : 1;
}
