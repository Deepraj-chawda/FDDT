"""
Shared PDF-page-to-image rasterization for the image-tampering checks
(ELA + copy-move — app/services/forensics/ela.py, copy_move.py).
app/tasks/tampering_checks_task.py renders once via this module and
hands the same in-memory page list to both checks, instead of each
re-rendering the PDF itself — the render is the expensive part.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np
import pymupdf

logger = logging.getLogger("fddt.render")

# 200 DPI balances enough pixel detail for ELA's recompression-error map
# and BRISK keypoint detection against memory/CPU cost across a
# multi-page document — in the same range as a real scanned page, so
# ELA's JPEG-block-artifact scale is representative of what a genuine
# scan/rescan would show (SPECIFICATION.md section 3.2 flags print-and-rescan
# as ELA's known weak spot; rendering far above real scan resolution
# would only make that worse).
RENDER_DPI = 200


@dataclass
class RenderedPage:
    """One page rendered once. `page_number` is 1-based — how a
    reviewer/PDF viewer counts pages, and how ela.py/copy_move.py's
    Finding descriptions and bounding_box.page refer to it — not the
    same as a 0-based list index. `has_image_content` gates ela.py's
    analysis (SPECIFICATION.md's "born-digital, text-native pages" exemption):
    true iff this PDF page has at least one embedded raster image
    XObject; copy_move.py ignores this flag and runs on every page (a
    duplicated paragraph/logo/signature shows up as a keypoint cluster
    whether the source content is a photo or rendered text/vector art)."""

    page_number: int
    image: np.ndarray  # BGR uint8, OpenCV's native layout
    width: int
    height: int
    has_image_content: bool


# ---------------------------------------------------------------------------
# Memory budget for rendered pages (workers only)
# ---------------------------------------------------------------------------
# A rendered page is held in memory for the whole check (an A4 page at 200 DPI
# is ~11.6 MB as BGR, ~2x that while it is being converted), and a worker runs
# several checks at once. A few long PDFs at the same time used to exceed the
# container's memory limit; the kernel then killed the worker (exit 137) and
# its in-flight checks were lost. Instead, each render reserves its estimated
# size from a per-process budget and WAITS while the budget is exhausted, so a
# burst of large documents is processed a few at a time rather than crashing.
# A document larger than the whole budget takes all of it and runs alone.
#
# The reservation is released when the Celery task ends (task_postrun, see
# app/tasks/celery_app.py), so callers just call render_pdf_pages(). It is
# active only inside a worker (enable_render_budget(), called at worker
# start); the API process and the tests render without a budget.

# Share of the container's memory limit that rendered pages may use; the rest
# is the interpreter, libraries, the PDF bytes and outgoing request payloads.
_BUDGET_FRACTION = 0.5
# Bytes held per rendered pixel: RGB samples + the BGR copy kept for checks.
_BYTES_PER_PIXEL = 3 * 2
_WAIT_LOG_SECONDS = 5.0


class _RenderBudget:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.in_use = 0
        self._cond = threading.Condition()
        self._held = threading.local()

    def held_by_this_thread(self) -> int:
        return getattr(self._held, "bytes", 0)

    def acquire(self, wanted: int) -> None:
        held = self.held_by_this_thread()
        need = max(0, min(wanted, self.capacity - held))
        if need == 0:
            return
        started = time.monotonic()
        logged = False
        with self._cond:
            while self.in_use + need > self.capacity:
                self._cond.wait(timeout=_WAIT_LOG_SECONDS)
                if not logged and time.monotonic() - started >= _WAIT_LOG_SECONDS:
                    logged = True
                    logger.info(
                        "render_budget_wait need_mb=%d in_use_mb=%d capacity_mb=%d",
                        need >> 20, self.in_use >> 20, self.capacity >> 20,
                    )
            self.in_use += need
        self._held.bytes = held + need

    def release_this_thread(self) -> None:
        held = self.held_by_this_thread()
        if not held:
            return
        self._held.bytes = 0
        with self._cond:
            self.in_use = max(0, self.in_use - held)
            self._cond.notify_all()


class _SharedRenderBudget:
    """The same budget shared by the processes of a prefork pool. Created in
    the pool's parent before it forks; the children inherit the shared memory.
    Each reservation is recorded against its process id, and a slot whose
    process has died no longer counts — a child killed mid-task (OOM, pool
    recycling) can't leak budget and starve the others. A prefork child runs
    one task at a time, so holdings are per process."""

    _POLL_SECONDS = 0.25

    def __init__(self, capacity: int, slots: int) -> None:
        import multiprocessing

        ctx = multiprocessing.get_context("fork")
        self.capacity = capacity
        self._lock = ctx.Lock()
        self._slots = ctx.Array("q", 2 * slots, lock=False)  # (pid, bytes) pairs

    def _pairs(self):
        for i in range(0, len(self._slots), 2):
            yield i, self._slots[i], self._slots[i + 1]

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @property
    def in_use(self) -> int:
        with self._lock:
            return sum(b for _, pid, b in self._pairs() if pid and self._alive(pid))

    def held_by_this_thread(self) -> int:
        me = os.getpid()
        return sum(b for _, pid, b in self._pairs() if pid == me)

    def acquire(self, wanted: int) -> None:
        me = os.getpid()
        started = time.monotonic()
        logged = False
        while True:
            with self._lock:
                used = mine = 0
                own = free = None
                for i, pid, held in self._pairs():
                    if pid and not self._alive(pid):
                        self._slots[i] = self._slots[i + 1] = 0  # reclaim a dead process's share
                        pid = held = 0
                    if pid == me:
                        own, mine = i, held
                    elif pid:
                        used += held
                    elif free is None:
                        free = i
                need = max(0, min(wanted, self.capacity - mine))
                if need == 0:
                    return
                slot = own if own is not None else free
                if slot is not None and used + mine + need <= self.capacity:
                    self._slots[slot], self._slots[slot + 1] = me, mine + need
                    return
            if not logged and time.monotonic() - started >= _WAIT_LOG_SECONDS:
                logged = True
                logger.info("render_budget_wait need_mb=%d in_use_mb=%d capacity_mb=%d",
                            need >> 20, (used + mine) >> 20, self.capacity >> 20)
            time.sleep(self._POLL_SECONDS)

    def release_this_thread(self) -> None:
        me = os.getpid()
        with self._lock:
            for i, pid, _ in self._pairs():
                if pid == me:
                    self._slots[i] = self._slots[i + 1] = 0


_budget: _RenderBudget | _SharedRenderBudget | None = None


def _container_memory_limit() -> int | None:
    """The cgroup memory limit in bytes, or None if unlimited/unknown."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = open(path, encoding="ascii").read().strip()
        except OSError:
            continue
        if raw.isdigit() and int(raw) < (1 << 60):  # cgroup v1 reports ~2^63 for "no limit"
            return int(raw)
        return None
    return None


def enable_render_budget(shared_processes: int = 0) -> int | None:
    """Turn the budget on: half the container's memory limit, or
    RENDER_MEMORY_BUDGET_MB. With `shared_processes` > 0 (a prefork pool, called
    in the parent before it forks) the budget is shared by that many child
    processes; otherwise it is shared by this process's threads. Returns the
    budget in bytes, or None when the container has no memory limit (nothing
    to protect against)."""
    global _budget
    override = os.environ.get("RENDER_MEMORY_BUDGET_MB", "").strip()
    if override.isdigit() and int(override) > 0:
        capacity = int(override) << 20
    else:
        limit = _container_memory_limit()
        if limit is None:
            _budget = None
            return None
        capacity = int(limit * _BUDGET_FRACTION)
    capacity = max(capacity, 64 << 20)
    if shared_processes > 0:
        _budget = _SharedRenderBudget(capacity, slots=shared_processes * 2 + 4)
    else:
        _budget = _RenderBudget(capacity)
    logger.info("render_budget_enabled capacity_mb=%d shared_processes=%d", capacity >> 20, shared_processes)
    return capacity


def release_render_budget() -> None:
    """Return this thread's reservation (called when its task ends)."""
    if _budget is not None:
        _budget.release_this_thread()


def estimated_render_bytes(doc: pymupdf.Document) -> int:
    zoom = RENDER_DPI / 72
    return sum(
        int(page.rect.width * zoom) * int(page.rect.height * zoom) * _BYTES_PER_PIXEL for page in doc
    )


def render_pdf_pages(pdf_bytes: bytes) -> list[RenderedPage]:
    pages: list[RenderedPage] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        if _budget is not None:
            _budget.acquire(estimated_render_bytes(doc))
        zoom = RENDER_DPI / 72  # a PDF's native unit is 1/72 inch
        matrix = pymupdf.Matrix(zoom, zoom)
        for index, page in enumerate(doc):
            pixmap = page.get_pixmap(matrix=matrix, colorspace=pymupdf.csRGB, alpha=False)
            rgb = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                pixmap.height, pixmap.width, pixmap.n
            )
            # pymupdf renders RGB; the checks built on top of this (both
            # adapted from cv2-based reference scripts) expect OpenCV's
            # native BGR layout.
            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            pages.append(
                RenderedPage(
                    page_number=index + 1,
                    image=bgr,
                    width=pixmap.width,
                    height=pixmap.height,
                    has_image_content=len(page.get_images(full=True)) > 0,
                )
            )
    return pages
