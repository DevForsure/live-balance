"""Weight-of-tablets PDF for a batch, from the switch presses in a balance's press log.

A batch is weighed in one or more cycles of 20 or 100 tablets. Each cycle starts from
a fresh tare and is weighed cumulatively: each press adds one tablet, so a tablet's
weight is this reading minus the previous one. Weights are logged in g, reported in mg,
to the balance's resolution: whole mg from a 0.000 g display, 0.1 mg from 0.0000 g.

- a new cycle starts when the reading drops to near zero (the balance was tared);
- a cycle belongs to the batch the camera read during it; one where no batch sheet
  was read is not counted (split_log);
- rejected_repeat presses are not tablets (pressed again, nothing added);
- nor is a press that rose less than NOTHING_ADDED of a typical tablet (empty pan, drift);
- a press without a readable weight is still a tablet, but its weight and the next
  tablet's are unknown (a blank slot each, to fill in by hand);
- total = the last readable cumulative reading, average = total / tablets up to it,
  so a missing reading in the middle does not change them;
- a cycle that is not exactly 20 or 100 tablets is flagged with the likely cause,
  never corrected automatically.

Allowable tolerance depends on the average weight (ported from v1):
    up to 80 mg       +-10 %
    81 - 250 mg       +-7.5 %
    above 250 mg      +-5 %
"""

from __future__ import annotations

import csv
import math
import os
import statistics
import struct
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]  # project folder (this file is in app/)
PER_ROW = 20  # tablet weights per printed row
CYCLE_SIZES = (20, 100)  # a cycle is one of these; anything else is wrong
NOT_TABLETS = {"rejected_repeat"}
# A press whose reading changed by less than this share of the cycle's typical tablet added
# nothing: switch pressed on the empty pan, or after the reading crept up (seen: +15 mg
# after a 3-minute pause, 65 mg tablets). Not a tablet; the next tablet counts from it.
NOTHING_ADDED = 0.4
NEW_CYCLE_TABLETS = 2.5  # a drop to below this many tablets' weight = re-tared

# PDF layout (mm). A cycle: two header lines, the balance readings (g), the method line,
# the tablet weights (mg), the totals line. The batch page mirrors place() in JavaScript
# for its sheet preview, so keep the two in step.
TOP_MM, BOTTOM_MM = 30, 20
HEAD_GAP, ROWS_GAP, ROW_H, LABEL_GAP, TOTAL_GAP, CYCLE_GAP = 5.5, 6, 5, 6, 7, 12
USABLE_MM = 297 - TOP_MM - BOTTOM_MM  # A4 height between the margins


def _block(rows: int) -> float:
    """Height (mm) of a cycle whose two grids have `rows` rows each: header .. totals line."""
    grid = ROWS_GAP + (rows - 1) * ROW_H
    return HEAD_GAP + grid + LABEL_GAP + grid + TOTAL_GAP


def block_mm(rep: "BatchReport") -> float:
    """Height of one cycle on the sheet: header .. totals line."""
    return _block(math.ceil(max(rep.expected, len(rep.tablets)) / PER_ROW))


SLOT_MM = _block(math.ceil(100 / PER_ROW)) + CYCLE_GAP
# places a print can start at = 100-tablet cycles that fit on a page (3 with two grids)
START_POSITIONS = int((USABLE_MM - _block(math.ceil(100 / PER_ROW))) // SLOT_MM) + 1


def place(heights: list[float], start: int = 1) -> tuple[list[tuple[int, float]], int]:
    """Where each cycle goes - (page, mm below the top margin) - starting at position
    `start`; a cycle that does not fit goes to the top of the next page. Also returns the
    position the next print can start at on the last page (1 = that page is full)."""
    off, page, spots = (start - 1) * SLOT_MM, 1, []
    for h in heights:
        if off + h > USABLE_MM and off > 0:
            page, off = page + 1, 0.0
        spots.append((page, off))
        off += h + CYCLE_GAP
    nxt = math.ceil(off / SLOT_MM - 1e-6) + 1  # first slot at or below where this print ended
    return spots, nxt if nxt <= START_POSITIONS else 1


def tolerance_pct(avg_mg: float) -> float:
    if avg_mg <= 80:
        return 10.0
    if avg_mg <= 250:
        return 7.5
    return 5.0


@dataclass
class Tablet:
    time: datetime
    reading_g: float | None  # cumulative display reading at this press
    weight_mg: float | None  # this tablet alone; None = blank slot
    status: str
    reading_txt: str = ""  # the reading exactly as logged from the balance display, e.g. "6.754"


@dataclass
class Skipped:
    """A press that added no tablet (NOTHING_ADDED)."""
    time: datetime
    reading_g: float
    change_mg: float          # vs the previous counted reading
    gap_sec: float | None     # since the previous press in the cycle; None = first press
    empty_pan: bool           # reading about zero; otherwise the reading crept up (or down)


@dataclass
class BatchReport:
    """One cycle of a batch."""
    batch: str
    tablets: list[Tablet]
    cycle: int = 1
    repeats: int = 0  # rejected_repeat presses in this cycle (not tablets)
    nothing_added: list["Skipped"] = field(default_factory=list)  # presses that added no tablet
    mg_places: int = 0  # decimals of mg the balance resolves (0.0000 g display -> 1)

    @property
    def expected(self) -> int:
        """20 or 100: whichever this cycle was meant to be."""
        return 20 if len(self.tablets) <= 60 else 100

    @property
    def complete(self) -> bool:
        return len(self.tablets) == self.expected

    @property
    def counted(self) -> int:
        """Tablets up to and including the last readable reading."""
        return max(
            (i for i, t in enumerate(self.tablets, 1) if t.reading_g is not None),
            default=0,
        )

    @property
    def total(self) -> float | None:
        """mg on the balance at the last readable press."""
        n = self.counted
        return to_mg(self.tablets[n - 1].reading_g, self.mg_places) if n else None

    @property
    def average(self) -> float | None:
        return self.total / self.counted if self.counted else None

    @property
    def tolerance(self) -> float | None:
        return tolerance_pct(self.average) if self.average is not None else None

    @property
    def limits(self) -> tuple[float, float] | None:
        if self.average is None:
            return None
        t = self.tolerance / 100
        return self.average * (1 + t), self.average * (1 - t)  # upper, lower

    def outside(self) -> list[tuple[int, Tablet]]:
        """(tablet number, tablet) for weights beyond the limits."""
        if not self.limits:
            return []
        hi, lo = self.limits
        return [
            (i, t)
            for i, t in enumerate(self.tablets, 1)
            if t.weight_mg is not None and not lo <= t.weight_mg <= hi
        ]

    def hints(self) -> list[str]:
        """Likely cause of a wrong tablet count, in plain words; [] when complete."""
        if self.complete:
            return []
        known = [t.weight_mg for t in self.tablets if t.weight_mg is not None]
        typical = statistics.median(known) if known else 0
        out = []
        for i, t in enumerate(self.tablets, 1):
            if t.weight_mg is None or not typical:
                continue
            if t.weight_mg >= 1.6 * typical:
                out.append(f"tablet #{i} = {t.weight_mg} mg, looks like 2 tablets in one press")
            elif t.weight_mg <= 0.4 * typical:
                out.append(f"tablet #{i} = {t.weight_mg} mg, looks like a press with nothing added")
        if not out:
            out.append("cycle stopped early" if len(self.tablets) < self.expected
                       else "more tablets weighed than the cycle needs")
        return out


def split_log(presses_csv: Path) -> tuple[dict[str, list[dict]], list[list[dict]]]:
    """(press rows per batch, cycles not counted).

    Nobody watches a screen while weighing, so the batch is settled per cycle after the
    fact: the log is cut into cycles (tare to tare) and each cycle takes the batch the
    camera read most often during it, for every press in it - including presses before
    the sheet was confirmed. A cycle in which no batch was read at all had no sheet
    there: it is not counted, only listed on the batch page."""
    return group_cycles(read_log(presses_csv)[1])


def read_log(presses_csv: Path) -> tuple[list[str], list[dict]]:
    """(column names, press rows) of the press log; ([], []) if there is none yet."""
    if not presses_csv.exists():
        return [], []
    with presses_csv.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def group_cycles(rows: list[dict]) -> tuple[dict[str, list[dict]], list[list[dict]]]:
    """split_log for rows already read."""
    out: dict[str, list[dict]] = {}
    uncounted = []
    for cyc in split_cycles(rows):
        seen = Counter(r["batch"] for r in cyc if r.get("batch"))
        if not seen:
            uncounted.append(cyc)
            continue
        batch = seen.most_common(1)[0][0]
        out.setdefault(batch, []).extend({**r, "batch": batch} for r in cyc)
    return out, uncounted


def batches(presses_csv: Path) -> dict[str, list[dict]]:
    """Press rows per batch (see split_log)."""
    return split_log(presses_csv)[0]


def read_product_names(path: Path) -> dict[str, str]:
    """Batch number -> product name from the batch list. Despite its .csv name it is a
    dBase table (columns B_NO, P_NAME); anything else raises, so the caller falls back."""
    data = path.read_bytes()
    if data[0] != 0x03:
        raise ValueError(f"{path} is not a dBase table")
    count, header_len, record_len = struct.unpack("<IHH", data[4:12])
    fields, pos = {}, 32  # name -> (offset in a record, width); byte 0 of a record is its deleted flag
    offset = 1
    while data[pos] != 0x0D:
        width = data[pos + 16]
        fields[data[pos:pos + 11].split(b"\0")[0].decode("ascii")] = (offset, width)
        offset, pos = offset + width, pos + 32
    (no_at, no_len), (name_at, name_len) = fields["B_NO"], fields["P_NAME"]
    names = {}
    for i in range(count):
        record = data[header_len + i * record_len:header_len + (i + 1) * record_len]
        batch = record[no_at:no_at + no_len].decode("cp1252").strip()
        name = record[name_at:name_at + name_len].decode("cp1252").strip()
        if record[:1] != b"*" and batch and name:
            names[batch] = name
    return names


def cycle_id(rep: "BatchReport") -> str:
    """Stable id of a cycle: its first tablet's press time (cycle numbers shift when a
    cycle is deleted; this does not)."""
    return rep.tablets[0].time.isoformat(timespec="milliseconds")


def latest_tablet_count(presses_csv: Path) -> int | None:
    """Tablets so far in the most recent cycle of the log (any batch), same counting as
    the reports; None if the log is empty."""
    rows = read_log(presses_csv)[1]
    return len(build("", split_cycles(rows)[-1]).tablets) if rows else None


def delete_cycle(presses_csv: Path, batch: str, cid: str, archive_csv: Path, by: str) -> int:
    """Move one cycle's presses (cycle_id `cid` of `batch`) out of the press log into
    archive_csv, stamped with when and by whom; later cycles are renumbered by
    themselves. Returns the number of presses moved, 0 if there is no such cycle.

    The recorder may append a press at any moment, so the log is only replaced when it
    has not changed since it was read; otherwise it is read again."""
    for _ in range(20):
        before = presses_csv.stat()
        fields, rows = read_log(presses_csv)
        target = next((c for c in split_cycles(group_cycles(rows)[0].get(batch, []))
                       if (rep := build(batch, c)).tablets and cycle_id(rep) == cid), None)
        if target is None:
            return 0
        times = {r["press_time"] for r in target}
        tmp = presses_csv.with_suffix(".deleting.tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fields)
            w.writeheader()
            w.writerows(r for r in rows if r["press_time"] not in times)
        now = presses_csv.stat()
        if (now.st_size, now.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            tmp.unlink()        # a press came in meanwhile: start over with it included
            time.sleep(0.1)
            continue
        try:
            os.replace(tmp, presses_csv)
        except PermissionError:  # the recorder has the log open this instant
            tmp.unlink(missing_ok=True)
            time.sleep(0.2)
            continue
        new = not archive_csv.exists()
        with archive_csv.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fields + ["deleted_at", "deleted_by"], extrasaction="ignore")
            if new:
                w.writeheader()
            stamp = datetime.now().isoformat(timespec="seconds")
            w.writerows({**r, "deleted_at": stamp, "deleted_by": by} for r in rows if r["press_time"] in times)
        return len(times)
    raise RuntimeError("the press log kept changing - try again")


def split_cycles(rows: list[dict]) -> list[list[dict]]:
    """Rows split where the balance was re-tared: the reading drops to below
    NEW_CYCLE_TABLETS tablets' weight, or by more than that (the next batch's tablet can
    weigh more than 2.5 of this one's: 190 mg after 67 mg tablets, seen live). A smaller
    drop (a tablet taken off mid-cycle) stays in the cycle and shows up as a tablet
    outside limits."""
    cycles: list[list[dict]] = []
    last, n = None, 0  # last reading in this cycle, tablets in it
    for r in sorted(rows, key=lambda r: r["press_time"]):
        w = float(r["weight"]) if r["weight"] else None
        if (not cycles or (w is not None and last is not None and n and w < last
                           and (w < NEW_CYCLE_TABLETS * last / n or last - w > NEW_CYCLE_TABLETS * last / n))):
            cycles.append([])
            last, n = None, 0
        cycles[-1].append(r)
        if r["status"] not in NOT_TABLETS:
            n += 1
            last = w if w is not None else last
    return cycles


def to_mg(g: float, places: int) -> float:
    """g -> mg rounded to `places` decimals (an int when 0)."""
    return round(g * 1000, places) if places else round(g * 1000)


def build(batch: str, rows: list[dict], cycle: int = 1) -> BatchReport:
    """One cycle's rows -> its report."""
    rows = sorted((r for r in rows), key=lambda r: r["press_time"])
    # mg decimals from how the balance displayed its readings ("0.0089" g -> 1 place)
    places = max((len(r["weight"].partition(".")[2]) - 3 for r in rows if r["weight"]), default=0)
    places = max(places, 0)
    # typical tablet: median rise between consecutive readable presses of this cycle
    readings = [float(r["weight"]) for r in rows if r["status"] not in NOT_TABLETS and r["weight"]]
    rises = [b - a for a, b in zip([0.0] + readings, readings) if b > a]
    typical = statistics.median(rises) if rises else None
    tablets, prev, repeats, skipped = [], 0.0, 0, []  # prev: last cumulative reading; None = unknown
    for i, r in enumerate(rows):
        if r["status"] in NOT_TABLETS:
            repeats += 1
            continue
        reading = float(r["weight"]) if r["weight"] else None
        # small change either way only: a clear drop (a tablet taken off) stays visible as a tablet outside limits
        if reading is not None and prev is not None and typical and abs(reading - prev) < NOTHING_ADDED * typical:
            t = datetime.fromisoformat(r["press_time"])
            gap = (t - datetime.fromisoformat(rows[i - 1]["press_time"])).total_seconds() if i else None
            skipped.append(Skipped(t, reading, to_mg(reading - prev, places), gap,
                                   empty_pan=reading < NOTHING_ADDED * typical))
            prev = reading  # not a tablet; the next tablet counts from here
            continue
        weight = (
            to_mg(reading - prev, places)
            if reading is not None and prev is not None
            else None
        )
        tablets.append(
            Tablet(
                datetime.fromisoformat(r["press_time"]), reading, weight, r["status"], r["weight"]
            )
        )
        prev = reading
    return BatchReport(batch, tablets, cycle, repeats, skipped, places)


def build_cycles(batch: str, rows: list[dict]) -> list[BatchReport]:
    reps = [build(batch, c, i) for i, c in enumerate(split_cycles(rows), 1)]
    return [r for r in reps if r.tablets]  # a cycle of only repeat presses has no tablets


QR_MM = 14  # QR code beside the two header lines: fits between the divider above and grid 1


def qr_text(rep: BatchReport, product: str, balance_id: str) -> str:
    """Summary of one cycle for its QR code (~120 characters: a 14 mm code holds it)."""
    first, last = rep.tablets[0].time, rep.tablets[-1].time
    hi, lo = rep.limits if rep.limits else (None, None)
    return "|".join([
        rep.batch, product, balance_id, f"{first:%d-%m-%Y %H:%M:%S}-{last:%H:%M:%S}",
        f"n={len(rep.tablets)}", f"tot={rep.total}mg", f"avg={rep.average:.2f}" if rep.average else "avg=",
        f"lim={lo:.2f}-{hi:.2f}" if hi else "lim=", f"out={len(rep.outside())}"])


def write_pdf(reps: list[BatchReport], out: str | Path, balance_id: str, product: str = "",
              start: int = 1, qr: bool = True) -> Path:
    """Selected cycles, one block each; a block that does not fit goes to the next page.
    balance_id: label of the balance they were weighed on (balance.Scale.balance_id).
    start (1..START_POSITIONS): where the first cycle goes on the first page, to continue
    a part-printed sheet; positions are the slots of 100-tablet cycles.
    qr: a QR code with the cycle's summary (qr_text) in each block's top-right corner."""
    if not 1 <= start <= START_POSITIONS:
        raise ValueError(f"start position must be 1-{START_POSITIONS}")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    batch_no = reps[0].batch
    c = canvas.Canvas(str(out), pagesize=A4)
    c.setTitle(f"Weight of tablets - batch {batch_no or '(not recorded)'}")
    W, H = A4
    left, right = 20 * mm, W - 20 * mm  # 170 mm wide: room for 20 balance readings like 6.754 per row
    top = H - TOP_MM * mm
    fs = 9.5

    # A line is a list of pieces: ("t", text) plain, ("v", value) bold on an underline.
    def line_w(pieces, size):
        w = 0.0
        for kind, s in pieces:
            w += (c.stringWidth(s, "Helvetica-Bold", size) + 8) if kind == "v" else c.stringWidth(s, "Helvetica", size) + 3
        return w

    def line_(x, y, pieces, size=fs):
        for kind, s in pieces:
            if kind == "v":
                w = c.stringWidth(s, "Helvetica-Bold", size)
                c.setFont("Helvetica-Bold", size)
                c.drawString(x + 1, y, s)
                c.line(x, y - 1.5, x + w + 2, y - 1.5)
                x += w + 8
            else:
                c.setFont("Helvetica", size)
                c.drawString(x, y, s)
                x += c.stringWidth(s, "Helvetica", size) + 3
        return x

    hdr_right = right - (QR_MM + 2) * mm if qr else right  # header lines make room for the QR code

    def header(y, left_pieces, right_pieces):
        """Left part at the left margin, right part ending at the right margin (as on the paper
        form); both shrink a little if they would otherwise touch."""
        size = min(fs, fs * (hdr_right - left - 8 * mm) / (line_w(left_pieces, fs) + line_w(right_pieces, fs)))
        line_(left, y, left_pieces, size)
        line_(hdr_right - line_w(right_pieces, size) + 5, y, right_pieces, size)

    def qr_code(y_line1, text):
        """QR_MM square at the right margin: top 5.5 mm above header line 1, bottom clear of grid 1."""
        widget = QrCodeWidget(text, barLevel="L", barBorder=2)  # L: bigger squares; clean printed paper needs little correction
        x0, y0, x1, y1 = widget.getBounds()
        d = Drawing(QR_MM * mm, QR_MM * mm, transform=[QR_MM * mm / (x1 - x0), 0, 0, QR_MM * mm / (y1 - y0), 0, 0])
        d.add(widget)
        renderPDF.draw(d, c, right - QR_MM * mm, y_line1 + 5.5 * mm - QR_MM * mm)

    slot_w = (right - left) / PER_ROW

    def grid(y, texts, underline=()):
        """PER_ROW values per row, each followed by a comma; returns the last row's y. The font
        is fitted to the widest value, so balance readings like 6.754 fit their slot too."""
        pad = 0.7 * mm  # space after each value's comma
        widest = max((c.stringWidth(t + ",", "Helvetica", 1) for t in texts if t), default=1)
        size = min(fs - 0.5, int((slot_w - pad - 0.2 * mm) / widest * 4) / 4)
        for i, t in enumerate(texts):
            if i and i % PER_ROW == 0:
                y -= ROW_H * mm
            x = left + (i % PER_ROW) * slot_w
            if t:
                if i in underline:
                    c.line(x + 0.5 * mm, y - 1.5, x + slot_w - pad + 0.5 * mm, y - 1.5)
                c.setFont("Helvetica", size)
                c.drawRightString(x + slot_w - pad, y, t + ",")
        return y

    spots, _ = place([block_mm(r) for r in reps], start)
    page = 1
    for rep, (p, off) in zip(reps, spots):
        slots = max(rep.expected, math.ceil(len(rep.tablets) / PER_ROW) * PER_ROW)
        if p != page:
            c.showPage()
            page = p
        y = top - off * mm
        if off > 0:  # divider centred between the last line above and this header's text
            c.setStrokeColor(colors.grey)
            c.setLineWidth(0.5)
            div_y = y + (CYCLE_GAP + 3) / 2 * mm
            c.line(left, div_y, right, div_y)
            c.setStrokeColor(colors.black)
            c.setLineWidth(1)

        first, last = rep.tablets[0].time, rep.tablets[-1].time
        header(y, [("t", "Batch No.:"), ("v", batch_no)], [("t", "Product Name:"), ("v", product or " " * 30)])
        if qr:
            qr_code(y, qr_text(rep, product, balance_id))
        y -= HEAD_GAP * mm
        header(y, [("t", "Weight in grams as per Balance ID.:"), ("v", balance_id)],
               [("t", "Date:"), ("v", f"{first:%d-%m-%Y}"), ("t", "from"), ("v", f"{first:%H:%M:%S}"),
                ("t", "to"), ("v", f"{last:%H:%M:%S}")])

        # 1. the readings exactly as the balance showed them (cumulative, g)
        y = grid(y - ROWS_GAP * mm, [t.reading_txt for t in rep.tablets] + [""] * (slots - len(rep.tablets)))
        y -= LABEL_GAP * mm
        c.setFont("Helvetica", fs)
        c.drawString(left, y, f"Weight of {len(rep.tablets)} Individual Tablet calculated using weight by "
                              "difference method from above, in mg.")

        # 2. each tablet alone (mg); blank slot = weight not known; outside the limits underlined
        hi, lo = rep.limits if rep.limits else (None, None)
        mg = [str(t.weight_mg) if t.weight_mg is not None else "" for t in rep.tablets]
        outside = {i for i, t in enumerate(rep.tablets)
                   if hi is not None and t.weight_mg is not None and not lo <= t.weight_mg <= hi}
        y = grid(y - ROWS_GAP * mm, mg + [""] * (slots - len(mg)), outside)
        y -= TOTAL_GAP * mm

        n = rep.counted
        tol = f"{rep.tolerance:g}" if rep.tolerance is not None else "____"
        totals = [("t", f"Total of {n} tablets"), ("v", str(rep.total) if n else ""), ("t", f"mg / {n} = avg."),
                  ("v", f"{rep.average:.2f}" if n else ""), ("t", f"mg  Tolerance ± {tol} %: upper limit"),
                  ("v", f"{rep.limits[0]:.2f}" if rep.limits else ""), ("t", "lower limit"),
                  ("v", f"{rep.limits[1]:.2f}" if rep.limits else "")]
        line_(left, y, totals, min(9, 9 * (right - left) / line_w(totals, 9)))  # shrink only if too long
    c.save()
    return out


if __name__ == "__main__":
    import re
    import sys
    from balance import DEFAULT, SCALES

    scale = SCALES[sys.argv[1] if len(sys.argv) > 1 else DEFAULT]
    out_dir = ROOT / "data" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    all_batches = batches(scale.press_csv)
    if not all_batches:
        print(f"No presses logged in {scale.press_csv}")
    for key, rows in all_batches.items():
        reps = build_cycles(key, rows)
        clean_name = re.sub(r"[^A-Za-z0-9-]+", "_", key)
        pdf_path = out_dir / f"batch_{clean_name}{scale.suffix}.pdf"
        write_pdf(reps, pdf_path, scale.balance_id)
        print(f"Generated: {pdf_path} ({len(reps)} cycle(s), "
              f"{sum(len(r.tablets) for r in reps)} tablets)")
