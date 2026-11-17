#!/usr/bin/env python3
"""Audit selected contents of an actual FIT recovery, offline.

Run on the host with Python 3 and permission to read the inputs and create the
output directory; no root or target connection is needed. Supply files from
the same build: --image is the FIT .itb, --kernel-image is its DECOMPRESSED
kernel payload (for example Image-initramfs), and --initramfs is the exact
embedded compressed or raw archive (for example usr/initramfs_inc_data).
For an in-kernel initramfs, the supplied archive bytes must occur exactly once
inside the kernel. For a separate FIT ramdisk, decompressed contents are
compared, so different compression of the same separate archive is allowed.
A nearby root directory cannot replace these operands. The FIT default
configuration is used unless explicitly selected.

--expect-file TARGET HOST_FILE compares exact raw bytes with a host file;
a differently stripped or transformed host file is not an exact match.
--package checks APK v3 metadata/file hashes and symlinks using the supplied
apk adbdump tool; it records dependency metadata, but does not solve package
dependencies or verify package signatures. --executable inspects an embedded
ELF's interpreter and needed libraries with readelf. Expected .ko files also
use readelf for vermagic checks. The selected musl loader is inspected; other
loader semantics are reported as unverified. This is not a runtime/dlopen test.
gzip/xz/lzma use Python; zstd/lz4 require explicitly named host decoders.
Declared FIT compression supports LZMA; supplied-archive detection does not
recognize raw LZMA. Only tools needed by actual inputs run, with C locale.
No tool is installed, no target binary is executed and no build is performed.

Example (run from the repository root):
  python3 scripts/check-image-content.py --image "$FIT" \\
    --kernel-image "$KERNEL" --initramfs "$ARCHIVE" \\
    --expect-file "$IMAGE_PATH" "$HOST_FILE" --readelf "$READELF" \\
    --package "$APK_FILE" --apk "$APK_TOOL" --zstd "$ZSTD" \\
    --executable "$IMAGE_ELF" --output "$OUT"
Set FIT/KERNEL/ARCHIVE/APK_FILE from the actual build artifacts, IMAGE_PATH
and IMAGE_ELF from paths inside that image, HOST_FILE from the file to compare,
and READELF/APK_TOOL/ZSTD from existing host tools. OUT must be a new directory.
Package/executable options can be omitted when those checks are not requested;
add --lz4 with its host tool if the selected payload uses lz4.
For a contents-only call, supply --image, --kernel-image, --initramfs and
--output, plus only the decoder actually needed. --timeout is a positive
number of seconds per command, not a deadline for the entire audit.

Read OUT/result.json first: errors, unverified, file/package comparisons and
ELF checks delimit the result. The decoded CPIO, extracts, package metadata
and dynamic readelf text remain in OUT, including after failure; module
modinfo stays in the report. Inputs and tools are borrowed and unchanged.
Exit 0 (matched) means the requested supported checks matched; exit 1 means a
recorded error/mismatch; exit 2 means unverified requested checks (or CLI usage
error, reported on stderr). FIT signature authenticity is always NOT checked.
Content correspondence does not establish source/build provenance, boot
success or loaded-module identity. Modes/owners are recorded; permission
policy belongs to check-openwrt-image-permissions.sh and is not run here.
Ctrl-C records interrupted and exits 130 after ending and collecting the
direct tool child. Repeated interruption or abrupt termination can leave
partial output or unconfirmed child ending; that is not a completed audit.
If final recording fails, the available report is printed to stderr and
cannot claim matched. No external state normally needs restoration."""

import argparse
from collections import Counter
import gzip
import hashlib
import json
import math
import lzma
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import struct
import subprocess
import sys
import time
import zlib


def run_command(argv, report, timeout, data=None):
    record = {"argv": [str(value) for value in argv], "start_ns": time.time_ns()}
    report["commands"].append(record)
    try:
        with subprocess.Popen(record["argv"], stdin=subprocess.PIPE if data is not None else None, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, "LC_ALL": "C"}) as child:
            try:
                stdout, stderr = child.communicate(data, timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt, OSError) as error:
                record["error"] = str(error) or "Interrupted"
                if isinstance(error, subprocess.TimeoutExpired):
                    record.update(timed_out=True, stdout=(error.stdout or b"").decode(errors="replace"), stderr=(error.stderr or b"").decode(errors="replace"))
                if isinstance(error, KeyboardInterrupt):
                    record["interrupted"] = True
                try:
                    child.kill()
                    stdout, stderr = child.communicate()
                    record.update(exit_code=child.returncode, stdout=stdout.decode(errors="replace"), stderr=stderr.decode(errors="replace"))
                except KeyboardInterrupt:
                    record["cleanup_error"] = "Interrupted again; child completion unconfirmed"
                    raise
                except OSError as cleanup_error:
                    record["cleanup_error"] = str(cleanup_error)
                raise
            record.update(exit_code=child.returncode, stderr=stderr.decode(errors="replace"))
            if child.returncode:
                raise RuntimeError(f"Tool failed ({child.returncode}): {record['argv']}: {record['stderr']}")
            return stdout
    except OSError as error:
        record.setdefault("error", str(error))
        raise
    finally:
        record["end_ns"] = time.time_ns()


def decompress(data, method, args, report):
    if method is None:
        if data.startswith(b"\x1f\x8b"):
            method = "gzip"
        elif data.startswith(b"\xfd7zXZ\0"):
            method = "xz"
        elif data.startswith(b"\x28\xb5\x2f\xfd"):
            method = "zstd"
        elif data.startswith((b"\x04\x22\x4d\x18", b"\x02\x21\x4c\x18")):
            method = "lz4"
        elif data.startswith((b"070701", b"070702")):
            method = "none"
        else:
            raise ValueError("Cannot identify supplied initramfs compression")
    if method == "none":
        return data
    if method == "gzip":
        return gzip.decompress(data)
    if method in ("xz", "lzma"):
        return lzma.decompress(data)
    if method in ("zstd", "lz4"):
        tool = getattr(args, method)
        if not tool:
            raise ValueError(f"Supply --{method} for this actual payload")
        return run_command([tool, "-d", "-c"], report, args.timeout, data)
    raise ValueError("Unsupported compression: " + method)


def read_fit(data, selected):
    if len(data) < 40:
        raise ValueError("Truncated FIT header")
    magic, total, structure, strings, reserved, version, compatible, cpu, string_size, structure_size = struct.unpack_from(">10I", data)
    if magic != 0xD00DFEED or total > len(data) or version < 17 or compatible > 17:
        raise ValueError("Invalid/unsupported FIT header")
    if structure + structure_size > total or strings + string_size > total:
        raise ValueError("FIT block outside declared FDT")
    names = data[strings : strings + string_size]
    nodes, stack = {}, []
    offset, end = structure, structure + structure_size
    finished = False
    while offset + 4 <= end:
        token = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        if token == 1:
            zero = data.find(b"\0", offset, end)
            if zero < 0:
                raise ValueError("Unterminated FIT node")
            name = data[offset:zero].decode("ascii")
            if "/" in name:
                raise ValueError("Invalid node name")
            stack.append(name)
            path = "/" + "/".join(stack[1:])
            if path in nodes:
                raise ValueError("Duplicate FIT node")
            nodes[path] = {}
            offset = (zero + 4) & ~3
        elif token == 2:
            if not stack:
                raise ValueError("Unbalanced FIT node")
            stack.pop()
        elif token == 3:
            if not stack or offset + 8 > end:
                raise ValueError("Truncated FIT property")
            length, name_offset = struct.unpack_from(">II", data, offset)
            offset += 8
            zero = names.find(b"\0", name_offset)
            if name_offset >= len(names) or zero < 0 or offset + length > end:
                raise ValueError("FIT property outside block")
            name = names[name_offset:zero].decode("ascii")
            props = nodes["/" + "/".join(stack[1:])]
            if name in props:
                raise ValueError("Duplicate FIT property")
            props[name] = data[offset : offset + length]
            offset = (offset + length + 3) & ~3
        elif token == 9:
            finished = not stack
            break
        elif token != 4:
            raise ValueError("Unknown FIT structure token")
    if not finished:
        raise ValueError("Missing FIT end or unclosed node")
    config_name = selected or nodes.get("/configurations", {}).get("default", b"").rstrip(b"\0").decode()
    config = nodes.get("/configurations/" + config_name)
    if not config or "kernel" not in config:
        raise ValueError("Selected FIT configuration has no kernel")
    payloads, checks = {}, []
    for role in ("kernel", "fdt", "ramdisk"):
        if role not in config:
            continue
        image_name = config[role].rstrip(b"\0").decode()
        path = "/images/" + image_name
        props = nodes[path]
        if "data" in props:
            payload = props["data"]
        else:
            size = int.from_bytes(props["data-size"], "big")
            position = int.from_bytes(props["data-position"], "big") if "data-position" in props else ((total + 3) & ~3) + int.from_bytes(props["data-offset"], "big")
            if position < total or position + size > len(data):
                raise ValueError("External FIT payload outside file")
            payload = data[position : position + size]
        hashes = []
        for child, values in nodes.items():
            if child.rsplit("/", 1)[0] != path or "algo" not in values:
                continue
            algo = values["algo"].rstrip(b"\0").decode()
            if algo not in ("crc32", "sha1", "sha256", "sha384", "sha512"):
                hashes.append({"algorithm": algo, "status": "unverified"})
                continue
            digest = struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF) if algo == "crc32" else hashlib.new(algo, payload).digest()
            if digest != values.get("value"):
                raise ValueError("FIT hash mismatch: " + child)
            hashes.append({"algorithm": algo, "status": "matched", "digest": digest.hex()})
        checks.append({"role": role, "node": path, "size": len(payload), "hashes": hashes})
        payloads[role] = (payload, props.get("compression", b"none\0").rstrip(b"\0").decode())
    return payloads, {"configuration": config_name, "payloads": checks, "signature_authenticity": "not_checked"}


def read_cpio(data):
    entries, hardlinks = {}, {}
    offset = 0
    while True:
        if offset + 110 > len(data) or data[offset : offset + 6] not in (b"070701", b"070702"):
            raise ValueError("Invalid or truncated newc header")
        crc = data[offset : offset + 6] == b"070702"
        fields = [int(data[offset + 6 + i * 8 : offset + 14 + i * 8], 16) for i in range(13)]
        inode, mode, uid, gid, nlink, mtime, size, major, minor, rmajor, rminor, namesize, checksum = fields
        start = offset + 110
        if namesize < 1 or start + namesize > len(data):
            raise ValueError("Invalid newc name length")
        name_bytes = data[start : start + namesize]
        if not name_bytes.endswith(b"\0") or b"\0" in name_bytes[:-1]:
            raise ValueError("Invalid newc name")
        name = name_bytes[:-1].decode().removeprefix("./")
        if name.startswith("/") or ".." in PurePosixPath(name).parts:
            raise ValueError("Escaping newc path")
        start = (start + namesize + 3) & ~3
        offset = (start + size + 3) & ~3
        if offset > len(data):
            raise ValueError("Truncated newc entry")
        payload = data[start : start + size]
        if crc and sum(payload) & 0xFFFFFFFF != checksum:
            raise ValueError("newc CRC mismatch")
        if name == "TRAILER!!!":
            if size or any(data[offset:]):
                raise ValueError("Unexpected data after newc trailer")
            break
        name = posixpath.normpath(name)
        if name in entries:
            raise ValueError("Duplicate normalized newc path: " + name)
        entry = {"mode": mode, "uid": uid, "gid": gid, "data": payload}
        entries[name] = entry
        if stat.S_ISLNK(mode):
            target = payload.removesuffix(b"\0")
            if not target or b"\0" in target:
                raise ValueError("Invalid symlink")
            entry["target"] = target.decode()
        if stat.S_ISREG(mode) and nlink > 1:
            hardlinks.setdefault((major, minor, inode), []).append(entry)
    for group in hardlinks.values():
        stored = [entry["data"] for entry in group if entry["data"]]
        if stored and any(value != stored[0] for value in stored):
            raise ValueError("Conflicting hardlink data")
        for entry in group:
            entry["data"] = stored[0] if stored else b""
    return entries


def resolve_image_path(entries, path):
    current = posixpath.normpath("/" + path.lstrip("/")).lstrip("/")
    links, visited = [], set()
    while True:
        if current in visited:
            return current, None, links, "Image symlink cycle at " + current
        visited.add(current)
        parts = current.split("/")
        for count in range(1, len(parts) + 1):
            prefix = "/".join(parts[:count])
            if prefix not in entries:
                return current, None, links, "Missing image component: " + prefix
            entry = entries[prefix]
            if stat.S_ISLNK(entry["mode"]):
                target = entry["target"]
                links.append({"path": prefix, "target": target})
                base = target if target.startswith("/") else posixpath.join("/", posixpath.dirname(prefix), target)
                current = posixpath.normpath(posixpath.join(base, *parts[count:])).lstrip("/")
                break
            if count < len(parts) and not stat.S_ISDIR(entry["mode"]):
                return current, None, links, "Non-directory image component: " + prefix
        else:
            return current, entry, links, None


def inspect_elf(entry, name, kind, args, report):
    if "inspection" not in entry:
        inspection = entry["inspection"] = {}
        try:
            saved = args.output / "extracted" / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(entry["data"])
            inspection["file"] = saved
        except OSError as error:
            inspection["error"] = str(error)
    inspection = entry["inspection"]
    if "error" in inspection:
        return inspection
    if kind in inspection:
        return inspection[kind]
    result = inspection[kind] = {}
    options = ["-h", "-l", "-d"] if kind == "dynamic" else ["-p", ".modinfo"]
    try:
        headers = run_command([args.readelf, *options, inspection["file"]], report, args.timeout).decode(errors="replace")
        if kind == "modinfo":
            result["text"] = headers
        else:
            evidence = args.output / "readelf" / name / "dynamic.txt"
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_text(headers)
            abi = [re.search(label + r":\s*(.+)", headers) for label in ("Machine", "Class", "Data")]
            if any(field is None for field in abi):
                raise ValueError("Missing ELF ABI field in readelf output: " + name)
            result.update(
                abi=tuple(field[1] for field in abi),
                evidence=str(evidence),
                interpreters=re.findall(r"Requesting program interpreter:\s*([^\]]+)\]", headers),
                needed=re.findall(r"\(NEEDED\).*\[([^\]]+)\]", headers),
                paths=re.findall(r"\(RUNPATH\).*\[([^\]]*)\]", headers) or re.findall(r"\(RPATH\).*\[([^\]]*)\]", headers),
            )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
    return result


def check_files(entries, args, report):
    repeated = {host: None for host, count in Counter(host for _, host in args.expect_file).items() if count > 1}
    for target, host in args.expect_file:
        check = {"target": target}
        report["checks"].append(check)
        resolved, entry, links, problem = resolve_image_path(entries, target)
        check.update(resolved=resolved, links=links)
        if problem:
            check["matched"] = False
            report["errors"].append(problem)
            continue
        check.update(mode=oct(entry["mode"]), uid=entry["uid"], gid=entry["gid"])
        if not stat.S_ISREG(entry["mode"]):
            check["matched"] = False
            report["errors"].append("Content/type mismatch: " + target)
            continue
        reading = repeated.get(host)
        if reading is None:
            try:
                expected = Path(host).read_bytes()
                reading = (expected, None)
                report["inputs"][host] = {"size": len(expected)}
            except OSError as error:
                reading = (None, str(error))
            if host in repeated:
                repeated[host] = reading
        expected, read_error = reading
        if read_error:
            report["errors"].append(f"Expected file {host} for {target}: {read_error}")
            continue
        check["matched"] = entry["data"] == expected
        if not check["matched"]:
            report["errors"].append("Content/type mismatch: " + target)
        if not target.endswith(".ko"):
            continue
        if not args.readelf:
            report["unverified"].append("Module vermagic (readelf not supplied): " + target)
            continue
        modinfo = inspect_elf(entry, resolved, "modinfo", args, report)
        if "error" in modinfo:
            report["errors"].append(modinfo["error"])
            continue
        check["modinfo"] = modinfo["text"]
        parts = PurePosixPath(target).parts
        if "modules" not in parts or parts.index("modules") + 1 >= len(parts):
            report["errors"].append("Module target has no installed kernel release directory: " + target)
            continue
        release = parts[parts.index("modules") + 1]
        if "vermagic=" + release + " " not in modinfo["text"]:
            report["errors"].append("Module vermagic differs from its installed release directory: " + target)


def check_packages(entries, args, report):
    repeated = {path: None for path, count in Counter(args.package).items() if count > 1}
    for index, package in enumerate(args.package):
        result = repeated.get(package)
        if result is not None:
            report["packages"].append(result)
            continue
        result = {"path": str(package), "files": []}
        report["packages"].append(result)
        if package in repeated:
            repeated[package] = result
        try:
            metadata = json.loads(run_command([args.apk, "adbdump", "--format", "json", package], report, args.timeout))
            (args.output / f"package-{index}.json").write_text(json.dumps(metadata, indent=2) + "\n")
            result["info"] = metadata["info"]
            for directory in metadata["paths"]:
                for item in directory.get("files", []):
                    name = posixpath.join(directory.get("name", ""), item["name"]).lstrip("/")
                    entry = entries.get(name)
                    matched = False
                    if entry is not None and stat.S_ISREG(entry["mode"]):
                        if "apk_digest" not in entry:
                            entry["apk_digest"] = hashlib.sha256(entry["data"]).hexdigest()
                        matched = item.get("size") == len(entry["data"]) and item.get("hash") == entry["apk_digest"]
                    elif entry is not None and stat.S_ISLNK(entry["mode"]):
                        encoded = bytes.fromhex(item.get("target", ""))
                        matched = len(encoded) >= 2 and int.from_bytes(encoded[:2], "little") == stat.S_IFLNK and encoded[2:] == entry["target"].encode()
                    result["files"].append({"path": name, "matched": matched})
                    if not matched:
                        report["errors"].append("APK/image mismatch: " + name)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, subprocess.SubprocessError) as error:
            report["errors"].append(f"APK {package}: {error}")


def check_elf_dependencies(entries, executable, system_paths, args, report):
    results = report["elf"][executable] = []
    name, entry, links, problem = resolve_image_path(entries, executable)
    if problem:
        results.append({"path": executable, "status": "failed"})
        report["errors"].append(problem)
        return
    pending, visited = [(name, entry, links, [])], set()
    expected_abi = None
    while pending:
        name, entry, links, inherited = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        result = {"path": name, "links": links, "dependencies": [], "status": "checking"}
        results.append(result)
        if not stat.S_ISREG(entry["mode"]) or not entry["data"].startswith(b"\x7fELF"):
            result["status"] = "failed"
            report["errors"].append("Requested ELF is not ELF: " + name)
            continue
        facts = inspect_elf(entry, name, "dynamic", args, report)
        if "error" in facts:
            result["status"] = "failed"
            report["errors"].append(facts["error"])
            continue
        if any("ld-musl-" not in value for value in facts["interpreters"]):
            result.update(status="unverified", reason="Non-musl loader semantics")
            report["unverified"].append("ELF loader scope: " + executable)
            continue
        if expected_abi is None:
            expected_abi = facts["abi"]
        elif facts["abi"] != expected_abi:
            result["status"] = "failed"
            report["errors"].append("ELF dependency ABI differs from its executable: " + name)
            continue
        result["machine"] = facts["abi"][0]
        chain = [(name, value) for value in facts["paths"]] + inherited
        directories, problem = [], None
        for owner, value in chain + [(name, system_paths)]:
            value = value.replace("${ORIGIN}", "/" + posixpath.dirname(owner)).replace("$ORIGIN", "/" + posixpath.dirname(owner))
            if "$" in value:
                problem = "Unresolved ELF search-path token: " + value
                break
            for directory in re.split("[:\n]", value):
                if not directory:
                    continue
                if not directory.startswith("/"):
                    problem = "ELF lookup depends on runtime cwd: " + directory
                    break
                directories.append(posixpath.normpath(directory).lstrip("/"))
            if problem:
                break
        if problem:
            result["status"] = "failed"
            report["errors"].append(problem)
            continue
        for dependency in facts["needed"] + facts["interpreters"]:
            if dependency.startswith("/"):
                choices = [dependency]
            elif "/" in dependency:
                result["status"] = "failed"
                report["errors"].append("Relative ELF dependency: " + dependency)
                continue
            else:
                choices = [directory + "/" + dependency for directory in directories]
            for candidate in choices:
                resolved, dep, chain_links, problem = resolve_image_path(entries, candidate)
                if dep is not None and stat.S_ISREG(dep["mode"]):
                    break
            else:
                result["status"] = "failed"
                report["errors"].append(f"Unresolved {dependency} needed by {name}")
                continue
            result["dependencies"].append({"name": dependency, "resolved": resolved, "links": chain_links})
            pending.append((resolved, dep, chain_links, chain))
        if result["status"] == "checking":
            result["status"] = "resolved"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", type=Path, required=True, help="Host FIT recovery .itb to inspect; not a rootfs directory")
    parser.add_argument("--kernel-image", type=Path, required=True, help="Host decompressed kernel payload matching the selected FIT kernel exactly")
    parser.add_argument("--initramfs", type=Path, required=True, help="Host file containing that kernel's exact embedded archive, or the selected FIT ramdisk archive")
    parser.add_argument("--configuration", help="FIT configuration node name; omitted uses /configurations default")
    parser.add_argument("--expect-file", nargs=2, action="append", default=[], metavar=("TARGET", "HOST_FILE"), help="Repeat: image-root TARGET path and expected HOST_FILE contents; .ko checks use --readelf")
    parser.add_argument("--package", type=Path, action="append", default=[], help="Repeat: host APK v3 file whose metadata and file contents are compared; requires --apk")
    parser.add_argument("--executable", action="append", default=[], help="Repeat: ELF path inside the image to check interpreter/NEEDED resolution; requires --readelf")
    parser.add_argument("--apk", help="Existing host apk executable supporting adbdump --format json; does not install packages")
    parser.add_argument("--readelf", help="Existing host readelf for target ELF dependencies and expected .ko vermagic")
    parser.add_argument("--zstd", help="Existing host zstd decoder when selected content uses zstd")
    parser.add_argument("--lz4", help="Existing host lz4 decoder when selected content uses lz4")
    parser.add_argument("--timeout", type=float, default=60, help="Seconds per external tool invocation, not a whole-analysis deadline (default: 60)")
    parser.add_argument("--output", type=Path, required=True, help="NEW host directory for result.json, extracted files and tool evidence")
    args = parser.parse_args()
    if not math.isfinite(args.timeout):
        parser.error("Tool timeout must be finite and positive")
    if args.timeout <= 0 or (args.package and not args.apk) or (args.executable and not args.readelf):
        parser.error("Supply positive timeout and tools needed by the requested checks")
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    report = {
        "inputs": {},
        "commands": [],
        "checks": [],
        "packages": [],
        "elf": {},
        "errors": [],
        "unverified": [],
        "arguments": {k: str(v) if isinstance(v, Path) else [str(p) for p in v] if k == "package" else v for k, v in vars(args).items()},
        "scope": "Selected artifact content; no runtime or hardware qualification; permission policy not assessed",
        "status": "running",
    }
    try:
        image = args.image.read_bytes()
        report["inputs"][str(args.image)] = {"size": len(image)}
        payloads, report["fit"] = read_fit(image, args.configuration)
        del image
        for payload in report["fit"]["payloads"]:
            if not payload["hashes"] or any(h["status"] == "unverified" for h in payload["hashes"]):
                report["unverified"].append("Hash coverage for " + payload["node"])
        kernel = decompress(*payloads["kernel"], args, report)
        expected_kernel = args.kernel_image.read_bytes()
        report["inputs"][str(args.kernel_image)] = {"size": len(expected_kernel)}
        if kernel != expected_kernel:
            raise ValueError("Actual FIT kernel differs from the supplied kernel image")
        del expected_kernel
        embedded = args.initramfs.read_bytes()
        report["inputs"][str(args.initramfs)] = {"size": len(embedded)}
        if "ramdisk" in payloads:
            actual = decompress(*payloads["ramdisk"], args, report)
            archive = decompress(embedded, None, args, report)
            if actual != archive:
                raise ValueError("FIT ramdisk differs from supplied initramfs")
            del actual
            report["initramfs_location"] = "selected FIT ramdisk"
        else:
            offset = kernel.find(embedded)
            if not embedded or offset < 0 or kernel.find(embedded, offset + 1) >= 0:
                raise ValueError("Exact embedded initramfs absent or ambiguous in FIT kernel")
            archive = decompress(embedded, None, args, report)
            report["initramfs_location"] = {"kernel_offset": offset, "size": len(embedded)}
        entries = read_cpio(archive)
        (args.output / "initramfs.cpio").write_bytes(archive)
        del archive, embedded, kernel, payloads
        check_files(entries, args, report)
        check_packages(entries, args, report)
        if args.executable:
            paths = [path for path in entries if re.fullmatch(r"etc/ld-musl-[^/]+\.path", path)]
            system_paths, problem = "/lib:/usr/local/lib:/usr/lib", None
            if len(paths) > 1:
                problem = "Ambiguous musl system path files"
            elif paths:
                name, entry, links, problem = resolve_image_path(entries, paths[0])
                if not problem:
                    system_paths = entry["data"].split(b"\0", 1)[0].decode()
            if problem:
                report["errors"].append(problem)
            else:
                for executable in args.executable:
                    check_elf_dependencies(entries, executable, system_paths, args, report)
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        report["errors"].append("Interrupted")
    except (OSError, ValueError, KeyError, RuntimeError, EOFError, struct.error, lzma.LZMAError, zlib.error, subprocess.SubprocessError) as error:
        report["errors"].append(str(error))
    finally:
        if report["status"] != "interrupted":
            report["status"] = "failed" if report["errors"] else "incomplete" if report["unverified"] else "matched"
        try:
            (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        except OSError as error:
            report["recording_error"] = str(error)
            if report["status"] != "interrupted":
                report["status"] = "failed"
            print(json.dumps(report, indent=2), file=sys.stderr)
    print(json.dumps({"status": report["status"], "errors": report["errors"], "unverified": report["unverified"], "evidence": str(args.output)}))
    if report["status"] == "interrupted":
        return 130
    return 1 if report["errors"] or report.get("recording_error") else 2 if report["unverified"] else 0


if __name__ == "__main__":
    sys.exit(main())
