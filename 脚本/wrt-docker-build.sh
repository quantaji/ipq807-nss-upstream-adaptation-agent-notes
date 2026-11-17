#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/wrt-docker-build.sh [command...]

Run an OpenWrt command inside Docker for the WXR-5950AX12 NSS worktree.
No arguments opens Bash. Supplied arguments are joined as Shell command text
and interpreted by container bash -lc; quote literal values inside that text.
The wrapper itself does not choose build targets or a parallel-job count.

Location and dependencies:
  The default host worktree is ../worktrees/openwrt-nss-adaptation-workrepo
  relative to the notes repository containing this script.
  Host: Bash, Docker CLI/daemon access, dirname, find, GNU stat, sort and cat.
  Container: Bash, mkdir and the selected OpenWrt build tools.
  Changed-image audits use the sibling check-openwrt-image-permissions.sh;
  its --help describes the required image-listing tools and recovery input.

Environment:
  WRT_IMAGE             One-shot docker run image. Default:
                        ghcr.io/openwrt/buildbot/buildworker-v3.8.0:v9
  WRT_CONTAINER         Existing running container for docker exec.
                        Set only one of WRT_CONTAINER and WRT_IMAGE.
  WRT_WORKTREE          Host worktree; relative paths use the caller's cwd.
  WRT_INNER             Container worktree path. Default:
                        /workspaces/openwrt-nss-adaptation-workrepo
  WRT_DOCKER_RUNTIME    docker run runtime. Default: runc.
  WRT_USER              Run: image user unless overridden. Exec: buildbot.
  WRT_HOME              Container HOME, created if needed. Default: /builder.
  WRT_NOFILE_LIMIT      Decimal descriptor count. Default: 4096.
                        Run sets Docker's soft/hard nofile limits; the inner
                        Shell's ulimit attempt is nonfatal in either mode.
  RECOVERY_CPIO         Exact recovery CPIO input; raw or xz. Relative paths
                        use caller cwd. See the permission checker's --help.

Run mode mounts WRT_WORKTREE at WRT_INNER. Docker removes the one-shot container
when its command ends; the mounted worktree remains.
Exec mode borrows WRT_CONTAINER; its WRT_INNER must already expose the same
worktree. Both modes run there with HOME=WRT_HOME and umask 022.
Docker uses -it when both stdin/stdout are terminals, otherwise -i.

Examples (run from the notes repository; nproc runs inside the container):
  scripts/wrt-docker-build.sh
  scripts/wrt-docker-build.sh 'make -j$(nproc) defconfig'
  scripts/wrt-docker-build.sh 'make -j$(nproc) V=s'
  WRT_IMAGE=my-openwrt-build-image scripts/wrt-docker-build.sh 'make -j$(nproc) defconfig'
  WRT_CONTAINER=openwrt-build scripts/wrt-docker-build.sh 'make -j$(nproc) target/linux/compile V=s'

  scripts/wrt-docker-build.sh 'printf "%s\n" "literal value; \$HOME"'

Results and stopping:
  Read command output and the final audit/skipped message first. Successful
  noninteractive commands audit changed WXR production/recovery outputs and
  explicit RECOVERY_CPIO. No/unchanged outputs or an incomplete image pair
  are reported as skipped; exit 0 alone does not mean permissions were audited.
  Invalid configuration exits 2. Docker, metadata and audit failures retain
  their actual nonzero result; failed commands are not followed by an audit.
  Partial artifacts are left in the worktree. Interactive calls have no audit.
  Leave interactive Bash with exit or Ctrl-D; Ctrl-C uses Docker's existing
  foreground handling. A borrowed container is never removed. Losing the
  client connection does not prove a command in that container has ended.
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PERMISSION_CHECK="$SCRIPT_DIR/check-openwrt-image-permissions.sh"

WRT_WORKTREE="${WRT_WORKTREE:-$(cd "$SCRIPT_DIR/../.." && pwd)/worktrees/openwrt-nss-adaptation-workrepo}"
WRT_INNER="${WRT_INNER:-/workspaces/openwrt-nss-adaptation-workrepo}"
WRT_DOCKER_RUNTIME="${WRT_DOCKER_RUNTIME:-runc}"
WRT_NOFILE_LIMIT="${WRT_NOFILE_LIMIT:-4096}"
WRT_CONTAINER="${WRT_CONTAINER:-}"
WRT_IMAGE="${WRT_IMAGE:-}"
WRT_USER="${WRT_USER:-}"
WRT_HOME="${WRT_HOME:-/builder}"

if [ -z "$WRT_CONTAINER" ]; then
  WRT_IMAGE="${WRT_IMAGE:-ghcr.io/openwrt/buildbot/buildworker-v3.8.0:v9}"
fi

case "$WRT_NOFILE_LIMIT" in
*[!0-9]*)
  echo "WRT_NOFILE_LIMIT must be a decimal descriptor count" >&2
  exit 2
  ;;
esac

WRT_WORKTREE="$(cd "$WRT_WORKTREE" && pwd)" || exit 2

if [ -n "$WRT_CONTAINER" ] && [ -n "$WRT_IMAGE" ]; then
  echo "Set only one of WRT_CONTAINER or WRT_IMAGE" >&2
  exit 2
fi

printf -v WRT_INNER_Q '%q' "$WRT_INNER"
printf -v WRT_HOME_Q '%q' "$WRT_HOME"

image_state() {
  local image_dir="$WRT_WORKTREE/bin/targets/qualcommax/ipq807x"

  {
    if [ -d "$image_dir" ]; then
      find "$image_dir" -maxdepth 1 -type f \
        \( -name '*-buffalo_wxr-5950ax12-squashfs-usb-rootfs.squashfs' \
        -o -name '*-buffalo_wxr-5950ax12-initramfs-uImage.itb' \) \
        -exec stat -c '%n|%i|%s|%y|%z' -- {} + || return $?
    fi
    if [ -n "${RECOVERY_CPIO:-}" ]; then
      if [ -f "$RECOVERY_CPIO" ]; then
        stat -c '%n|%i|%s|%y|%z' -- "$RECOVERY_CPIO" || return $?
      else
        printf '%s|missing\n' "$RECOVERY_CPIO"
      fi
    fi
  } | sort
}

INNER_CMD="cd $WRT_INNER_Q && umask 022 && mkdir -p $WRT_HOME_Q && { ulimit -n $WRT_NOFILE_LIMIT 2>/dev/null || true; } && "
if [ "$#" -eq 0 ]; then
  INNER_CMD+="exec bash"
else
  INNER_CMD+="$*"
fi

if [ -t 0 ] && [ -t 1 ]; then
  tty_args=(-it)
else
  tty_args=(-i)
fi

if [ -n "$WRT_CONTAINER" ]; then
  WRT_USER="${WRT_USER:-buildbot}"
  docker_args=(exec "${tty_args[@]}"
    -u "$WRT_USER"
    -e "HOME=$WRT_HOME"
    "$WRT_CONTAINER")
else
  docker_args=(run --rm "${tty_args[@]}"
    --runtime "$WRT_DOCKER_RUNTIME"
    --ulimit "nofile=$WRT_NOFILE_LIMIT:$WRT_NOFILE_LIMIT")
  if [ -n "$WRT_USER" ]; then
    docker_args+=(--user "$WRT_USER")
  fi
  docker_args+=(-e "HOME=$WRT_HOME"
    -v "$WRT_WORKTREE:$WRT_INNER"
    -w "$WRT_INNER"
    "$WRT_IMAGE")
fi
docker_args+=(bash -lc "$INNER_CMD")

if [ "$#" -eq 0 ]; then
  exec docker "${docker_args[@]}"
fi

IMAGE_STATE_BEFORE="$(image_state)"
docker "${docker_args[@]}"
IMAGE_STATE_AFTER="$(image_state)"

if [ -z "$IMAGE_STATE_AFTER" ]; then
  echo "No final WXR USB rootfs or recovery artifact exists; permission audit skipped."
elif [ "$IMAGE_STATE_AFTER" = "$IMAGE_STATE_BEFORE" ]; then
  echo "No final WXR image or explicit recovery CPIO changed; permission audit skipped."
else
  WRT_WORKTREE="$WRT_WORKTREE" "$PERMISSION_CHECK" --if-present
fi
