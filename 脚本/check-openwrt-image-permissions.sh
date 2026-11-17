#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/check-openwrt-image-permissions.sh [--if-present]

On the host, audit the final WXR-5950AX12 USB production SquashFS and the
CPIO used by its canonical initramfs recovery FIT. Public directories must
be traversable, public files readable, and owner-executable files executable
by non-root services. Existing root-only configuration exceptions apply.
Symlinks and special files are not checked. No root or router access is needed.

Environment:
  WRT_WORKTREE    Host OpenWrt worktree. Default: the sibling worktree
                  ../worktrees/openwrt-nss-adaptation-workrepo of the notes repo.
  RECOVERY_CPIO   Exact CPIO input used by the selected recovery FIT (raw or xz).
                  Relative paths are resolved from the caller's working directory.
                  Automatic lookup uses an ordinary embedded initramfs build;
                  per-device, separate or external initramfs needs this input.

Dependencies:
  Bash and host dirname/find/sort/grep/mktemp/rm/cat; the selected worktree's
  staging_dir/host/bin/unsquashfs4 and cpio, plus xz for a *.xz recovery input.

Results:
  --if-present skips only when the final production/recovery pair is absent.
  Once both images exist, missing or ambiguous audit inputs are errors.
  Read stdout for passed/skipped results and stderr for findings/tool errors.
  Findings keep listing order; each violating entry is counted once, followed
  by the failure total. Production failure stops before the recovery audit.
  Exit 0 means passed or explicitly skipped; 1 means selection/format/permission
  failure; 2 means usage/worktree configuration failure. Tool failures retain
  their nonzero status. An interrupted audit is not a pass.
  Temporary listings are removed by the exit trap; no persistent output is
  created. Forced termination can prevent cleanup. Images are never modified.
  This checks permissions, not FIT hashes, GPT layout, boot or deployment.

Examples (run from the notes repository):
  scripts/check-openwrt-image-permissions.sh
  WRT_WORKTREE="$OPENWRT_TREE" scripts/check-openwrt-image-permissions.sh --if-present
  RECOVERY_CPIO="$EXACT_RECOVERY_CPIO" scripts/check-openwrt-image-permissions.sh
  Set OPENWRT_TREE to the actual build worktree and EXACT_RECOVERY_CPIO to the
  raw or .xz archive used by that recovery FIT, not a nearby filesystem tree.
EOF
}

IF_PRESENT=0
case "${1:-}" in
'') ;;
--if-present)
  IF_PRESENT=1
  ;;
-h | --help)
  usage
  exit 0
  ;;
*)
  usage >&2
  exit 2
  ;;
esac

if [ "$#" -gt 1 ]; then
  usage >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTES_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_WORKTREE="$(cd "$NOTES_ROOT/.." && pwd)/worktrees/openwrt-nss-adaptation-workrepo"
WRT_WORKTREE="${WRT_WORKTREE:-$DEFAULT_WORKTREE}"

if ! WRT_WORKTREE="$(cd "$WRT_WORKTREE" && pwd)"; then
  echo "Cannot enter the OpenWrt worktree." >&2
  exit 2
fi

BUILD_DIR="$WRT_WORKTREE/build_dir"
IMAGE_DIR="$WRT_WORKTREE/bin/targets/qualcommax/ipq807x"
UNSQUASHFS="$WRT_WORKTREE/staging_dir/host/bin/unsquashfs4"
CPIO="$WRT_WORKTREE/staging_dir/host/bin/cpio"
XZ="$WRT_WORKTREE/staging_dir/host/bin/xz"

ROOTFS_IMAGES=()
RECOVERY_FITS=()
if [ -d "$IMAGE_DIR" ]; then
  ROOTFS_PATHS="$(find "$IMAGE_DIR" -maxdepth 1 -type f \
    -name '*-buffalo_wxr-5950ax12-squashfs-usb-rootfs.squashfs' -print | sort)"
  RECOVERY_FIT_PATHS="$(find "$IMAGE_DIR" -maxdepth 1 -type f \
    -name '*-buffalo_wxr-5950ax12-initramfs-uImage.itb' -print | sort)"
  if [ -n "$ROOTFS_PATHS" ]; then
    mapfile -t ROOTFS_IMAGES <<<"$ROOTFS_PATHS"
  fi
  if [ -n "$RECOVERY_FIT_PATHS" ]; then
    mapfile -t RECOVERY_FITS <<<"$RECOVERY_FIT_PATHS"
  fi
fi

if [ "${#ROOTFS_IMAGES[@]}" -eq 0 ] || [ "${#RECOVERY_FITS[@]}" -eq 0 ]; then
  if [ "$IF_PRESENT" -eq 1 ]; then
    echo "The final WXR USB rootfs/recovery pair is incomplete; permission audit skipped."
    exit 0
  fi
  echo "Both a final WXR USB SquashFS and canonical recovery FIT are required in: $IMAGE_DIR" >&2
  exit 1
fi

if [ "${#ROOTFS_IMAGES[@]}" -ne 1 ]; then
  echo "Expected exactly one final WXR USB SquashFS; found ${#ROOTFS_IMAGES[@]}:" >&2
  printf '  %s\n' "${ROOTFS_IMAGES[@]}" >&2
  exit 1
fi
if [ "${#RECOVERY_FITS[@]}" -ne 1 ]; then
  echo "Expected exactly one canonical WXR recovery FIT; found ${#RECOVERY_FITS[@]}:" >&2
  printf '  %s\n' "${RECOVERY_FITS[@]}" >&2
  exit 1
fi
if [ "${ROOTFS_IMAGES[0]%-squashfs-usb-rootfs.squashfs}" != \
  "${RECOVERY_FITS[0]%-initramfs-uImage.itb}" ]; then
  echo "The WXR USB rootfs and recovery FIT have different build/device prefixes." >&2
  exit 1
fi

if [ -n "${RECOVERY_CPIO:-}" ]; then
  RECOVERY_IMAGES=("$RECOVERY_CPIO")
else
  if ! grep -qx 'CONFIG_TARGET_ROOTFS_INITRAMFS=y' "$WRT_WORKTREE/.config"; then
    echo "Cannot identify the embedded recovery CPIO from the current OpenWrt configuration." >&2
    echo "Set RECOVERY_CPIO to the exact archive used by the canonical recovery FIT." >&2
    exit 1
  fi
  if grep -Eq '^(CONFIG_TARGET_PER_DEVICE_ROOTFS|CONFIG_TARGET_ROOTFS_INITRAMFS_SEPARATE)=y$|^CONFIG_EXTERNAL_CPIO="[^"]+' \
    "$WRT_WORKTREE/.config"; then
    echo "Automatic CPIO lookup cannot identify this per-device, separate or external initramfs." >&2
    echo "Set RECOVERY_CPIO to its actual archive; a generic kernel CPIO is not a substitute." >&2
    exit 1
  elif [ "$?" -ne 1 ]; then
    exit 1
  fi
  RECOVERY_IMAGES=()
  if [ -d "$BUILD_DIR" ]; then
    RECOVERY_PATHS="$(find "$BUILD_DIR" -type f \
      -path '*/linux-qualcommax_ipq807x/linux-*/usr/initramfs_data.cpio' -print | sort)"
    if [ -n "$RECOVERY_PATHS" ]; then
      mapfile -t RECOVERY_IMAGES <<<"$RECOVERY_PATHS"
    fi
  fi
fi

if [ "${#RECOVERY_IMAGES[@]}" -ne 1 ]; then
  echo "Expected exactly one CPIO for the canonical recovery; found ${#RECOVERY_IMAGES[@]}:" >&2
  if [ "${#RECOVERY_IMAGES[@]}" -gt 0 ]; then
    printf '  %s\n' "${RECOVERY_IMAGES[@]}" >&2
  fi
  echo "Set RECOVERY_CPIO to the exact archive used by the canonical recovery FIT." >&2
  exit 1
fi

TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

audit_listing() {
  local label="$1"
  local listing="$2"
  local metadata_fields="$3"
  local prefix="$4"
  local line mode path kind
  local violation_count=0
  # The producer writes one space before the path. Preserve all later spaces.
  local entry_pattern="^(.{10})([[:blank:]]+[^[:blank:]]+){${metadata_fields}} (.*)$"

  while IFS= read -r line; do
    case "${line:0:1}" in
    '' | l | b | c | p | s)
      continue
      ;;
    - | d) ;;
    *)
      printf '%s cannot parse permission listing entry: %s\n' "$label" "$line" >&2
      return 1
      ;;
    esac

    if [[ ! "$line" =~ $entry_pattern ]]; then
      printf '%s cannot parse permission listing entry: %s\n' "$label" "$line" >&2
      return 1
    fi
    mode="${BASH_REMATCH[1]}"
    path="${BASH_REMATCH[3]}"
    path="${path#"$prefix"}"
    path="${path#./}"

    # dnsmasq configuration and trust anchors use INSTALL_CONF (0600) and
    # are read by the root-owned startup path.
    case "$path" in
    '' | . | root | root/* | etc/config | etc/config/* | \
      etc/ssl/private | etc/ssl/private/* | etc/shadow | etc/ppp/chap-secrets | \
      etc/dnsmasq.conf | etc/hotplug-preinit.json | etc/hotplug.json | \
      etc/hotplug.d/block/00-media-change | etc/hotplug.d/block/10-mount | \
      etc/odhcp6c.user | etc/uci-defaults/10-fstab | \
      usr/share/dnsmasq/dhcpbogushostname.conf | usr/share/dnsmasq/rfc6761.conf | \
      usr/share/dnsmasq/trust-anchors.conf)
      continue
      ;;
    esac

    kind=''
    if [[ "${mode:0:1}" = d ]]; then
      if [[ "${mode:9:1}" != [xt] ]]; then
        kind=DIR_TRAVERSE
      fi
    elif [[ "${mode:3:1}" = [xs] ]]; then
      if [[ "${mode:7:1}" != r || "${mode:9:1}" != [xt] ]]; then
        kind=FILE_EXEC_ACCESS
      fi
    elif [[ "${mode:7:1}" != r ]]; then
      kind=FILE_READ
    fi

    if [ -z "$kind" ]; then
      continue
    fi
    if [ "$violation_count" -eq 0 ]; then
      printf '%s permission violations:\n' "$label" >&2
      echo "TYPE              MODE        PATH" >&2
    fi
    violation_count=$((violation_count + 1))
    printf '%-17s %-11s /%s\n' "$kind" "$mode" "$path" >&2
  done <"$listing"

  if [ "$violation_count" -gt 0 ]; then
    printf '%s permission audit failed: %s violation(s).\n' "$label" "$violation_count" >&2
    return 1
  fi
  echo "$label permission audit passed."
}

SQUASHFS_LISTING="$TEMP_DIR/production-squashfs.list"
RECOVERY_LISTING="$TEMP_DIR/recovery-cpio.list"

"$UNSQUASHFS" -lln "${ROOTFS_IMAGES[0]}" >"$SQUASHFS_LISTING"

if [[ "${RECOVERY_IMAGES[0]}" = *.xz ]]; then
  "$XZ" -dc "${RECOVERY_IMAGES[0]}" | "$CPIO" -itvn >"$RECOVERY_LISTING"
else
  "$CPIO" -itvn <"${RECOVERY_IMAGES[0]}" >"$RECOVERY_LISTING"
fi

audit_listing "USB production SquashFS" "$SQUASHFS_LISTING" 4 "squashfs-root/"
audit_listing "Recovery CPIO" "$RECOVERY_LISTING" 7 ""

echo "Final filesystem permission audit passed:"
printf '  production:   %s\n' "${ROOTFS_IMAGES[0]}"
printf '  recovery FIT: %s\n' "${RECOVERY_FITS[0]}"
printf '  recovery CPIO: %s\n' "${RECOVERY_IMAGES[0]}"
