"""Scan a running MTGA.exe process for the owned-card collection dictionary.

The collection is a Mono `Dictionary<int,int>` (grpId -> count) living in
the client's managed heap. This module locates it by:

  1. Enumerating rw memory regions via /proc/<pid>/maps.
  2. Reading each region via /proc/<pid>/mem.
  3. Scanning for two candidate layouts (see below).
  4. Returning ranked candidate blocks to the caller for anchor scoring.

Layout 1 — Mono Dictionary entries (16 bytes each):
    (int hashCode, int next, int key, int value)
    For int keys, hashCode == key. `next` is either -1 (0xFFFFFFFF) or a
    small index. We match on `words[i] == words[i+2]` plus range checks.

Layout 2 — Packed pairs (8 bytes each):
    (int key, int value)
    Fallback for a `List<KeyValuePair<int,int>>` or custom struct.

Anchor scoring lives in `anchors.py` — this module is layout-agnostic
and just yields candidate {grpId: count} dicts with their source
addresses.
"""

from __future__ import annotations

import array
import os
import re
from dataclasses import dataclass

# Heuristic bounds for what a plausible grpId and owned-count look like.
GRP_MIN, GRP_MAX = 1000, 500_000
COUNT_MIN, COUNT_MAX = 1, 400
# A real collection has hundreds+ entries; require this many valid pairs
# in a row before considering it a candidate block. Configurable via CLI.
DEFAULT_MIN_BLOCK = 40


def find_mtga_pid() -> int | None:
    """Return the PID of the running MTGA.exe Wine process, or None."""
    candidates: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm") as f:
                comm = f.read().strip()
        except OSError:
            continue
        if comm == "MTGA.exe":
            candidates.append(int(entry))

    if not candidates:
        return None
    return max(candidates, key=_rss)


def _rss(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/statm") as f:
            return int(f.read().split()[1])
    except OSError:
        return 0


@dataclass
class Region:
    start: int
    end: int
    perms: str
    path: str

    @property
    def size(self) -> int:
        return self.end - self.start


_MAP_RE = re.compile(
    r"([0-9a-f]+)-([0-9a-f]+)\s+(\S+)\s+\S+\s+\S+\s+\S+\s*(.*)"
)


def read_maps(pid: int) -> list[Region]:
    """Return rw-p regions from /proc/<pid>/maps, skipping shared libs."""
    regions: list[Region] = []
    with open(f"/proc/{pid}/maps") as f:
        for line in f:
            m = _MAP_RE.match(line)
            if not m:
                continue
            perms = m.group(3)
            if "r" not in perms or "w" not in perms:
                continue
            path = m.group(4).strip()
            if path.startswith("/") and path.endswith(".so"):
                continue
            if path in ("[vvar]", "[vdso]", "[vsyscall]"):
                continue
            regions.append(Region(
                start=int(m.group(1), 16),
                end=int(m.group(2), 16),
                perms=perms,
                path=path,
            ))
    return regions


class MemReader:
    """Random-access reader over /proc/<pid>/mem.

    Raises PermissionError on open if `kernel.yama.ptrace_scope != 0` and
    caller lacks CAP_SYS_PTRACE. See README for the fix.
    """

    def __init__(self, pid: int):
        self.pid = pid
        try:
            self._fd = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
        except PermissionError as e:
            raise PermissionError(
                f"cannot open /proc/{pid}/mem — "
                "run `sudo sysctl kernel.yama.ptrace_scope=0` or use sudo"
            ) from e

    def read(self, offset: int, size: int) -> bytes:
        try:
            return os.pread(self._fd, size, offset)
        except OSError:
            return b""

    def close(self) -> None:
        os.close(self._fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _as_u32(buf: bytes) -> array.array:
    n = len(buf) - (len(buf) % 4)
    a = array.array("I")
    a.frombytes(buf[:n])
    return a


def scan_dict_entries(
    buf: bytes, min_block: int = DEFAULT_MIN_BLOCK
) -> list[tuple[int, dict[int, int]]]:
    """Yield candidate Mono Dictionary<int,int> blocks.

    Each yield is (byte_offset_in_buf, {grpId: count}).
    """
    hits: list[tuple[int, dict[int, int]]] = []
    words = _as_u32(buf)
    L = len(words)

    i = 0
    while i + 4 <= L:
        h, nxt, key, val = words[i], words[i + 1], words[i + 2], words[i + 3]
        if (
            h == key
            and GRP_MIN <= key < GRP_MAX
            and COUNT_MIN <= val <= COUNT_MAX
            and (nxt == 0xFFFFFFFF or nxt < 0x100000)
        ):
            run: dict[int, int] = {key: val}
            j = i + 4
            while j + 4 <= L:
                h2, nxt2, k2, v2 = words[j], words[j + 1], words[j + 2], words[j + 3]
                if not (
                    h2 == k2
                    and GRP_MIN <= k2 < GRP_MAX
                    and COUNT_MIN <= v2 <= COUNT_MAX
                    and (nxt2 == 0xFFFFFFFF or nxt2 < 0x100000)
                ):
                    break
                if k2 in run:
                    break
                run[k2] = v2
                j += 4
            if len(run) >= min_block:
                hits.append((i * 4, run))
                i = j
                continue
        i += 1
    return hits


def scan_packed_pairs(
    buf: bytes, min_block: int = DEFAULT_MIN_BLOCK
) -> list[tuple[int, dict[int, int]]]:
    """Yield candidate packed (key, value) blocks (8 bytes each)."""
    hits: list[tuple[int, dict[int, int]]] = []
    words = _as_u32(buf)
    L = len(words)

    i = 0
    while i + 2 <= L:
        key, val = words[i], words[i + 1]
        if GRP_MIN <= key < GRP_MAX and COUNT_MIN <= val <= COUNT_MAX:
            run: dict[int, int] = {key: val}
            j = i + 2
            while j + 2 <= L:
                k2, v2 = words[j], words[j + 1]
                if not (GRP_MIN <= k2 < GRP_MAX and COUNT_MIN <= v2 <= COUNT_MAX):
                    break
                if k2 in run:
                    break
                run[k2] = v2
                j += 2
            if len(run) >= min_block:
                hits.append((i * 4, run))
                i = j
                continue
        i += 1
    return hits


@dataclass
class Candidate:
    kind: str            # "dict" or "packed"
    region_start: int    # base VA of the region the block came from
    offset: int          # byte offset within that region
    block: dict[int, int]

    @property
    def address(self) -> int:
        return self.region_start + self.offset

    @property
    def entries(self) -> int:
        return len(self.block)


def scan_process(
    pid: int,
    min_block: int = DEFAULT_MIN_BLOCK,
    max_region_bytes: int = 512 * 1024 * 1024,
    progress=None,
) -> list[Candidate]:
    """Full pipeline: enumerate regions, read, scan, return candidates.

    `progress(current_region_idx, total_regions)` is called before each
    region if provided.
    """
    regions = read_maps(pid)
    candidates: list[Candidate] = []
    with MemReader(pid) as mem:
        for idx, r in enumerate(regions):
            if progress:
                progress(idx, len(regions))
            if r.size > max_region_bytes:
                continue
            buf = mem.read(r.start, r.size)
            if not buf:
                continue
            for off, block in scan_dict_entries(buf, min_block):
                candidates.append(Candidate("dict", r.start, off, block))
            for off, block in scan_packed_pairs(buf, min_block):
                candidates.append(Candidate("packed", r.start, off, block))
    return candidates
