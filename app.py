import re
import sqlite3
from datetime import datetime
from io import BytesIO
from pathlib import Path

from fpdf import FPDF
from flask import Flask, abort, g, redirect, render_template, request, send_file, url_for

app = Flask(__name__)

DB_PATH = Path(__file__).parent / "time_log.db"
STATUSES = ["Not Started", "In Progress", "Completed", "Blocked"]
SORT_COLUMNS = {"date": "date", "task": "task", "hours": "hours", "status": "status"}
MONTH_KEY_RE = re.compile(r"^\d{4}-\d{2}$")

BRAND_RGB = (60, 110, 88)
STATUS_RGB = {
    "Not Started": (71, 85, 105),
    "In Progress": (146, 64, 14),
    "Completed": (22, 108, 67),
    "Blocked": (176, 42, 55),
}

SEED_ENTRIES = [
    ("2026-06-01", "work on the program API, Save program history", 3, "Completed"),
    ("2026-06-02", "mention on comment, delete files by admin, order logic", 4, "Completed"),
    ("2026-06-03", "comment reaction and chat", 1, "Completed"),
    ("2026-06-08", "Reaction on Nested comments and delete comment logic", 2, "Completed"),
    ("2026-06-09", "Rename chat sessions and share logic", 3, "Completed"),
    ("2026-06-10", "work on chat share logic", 3, "Completed"),
    ("2026-06-11", "Add member approval logic", 3, "Completed"),
    ("2026-06-12", "Dynamic search for invite", 1, "Completed"),
    ("2026-06-13", "Discoverable and not discoverable", 2, "Completed"),
    ("2026-06-14", "Work on details API", 2, "Completed"),
    ("2026-06-15", "Fix the broken model", 3, "Completed"),
    ("2026-06-16", "Deploy the code in prod", 1, "Completed"),
    ("2026-06-18", "work on custom RAG AI", 4, "In Progress"),
    ("2026-06-19", "work on API", 4, "In Progress"),
]


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            task TEXT NOT NULL,
            hours REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'Not Started'
        )
        """
    )
    count = db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    if count == 0:
        db.executemany(
            "INSERT INTO entries (date, task, hours, status) VALUES (?, ?, ?, ?)",
            SEED_ENTRIES,
        )
        db.commit()
    db.close()


def _fit_text(pdf, text, width, font_size):
    pdf.set_font("Helvetica", "", font_size)
    if pdf.get_string_width(text) <= width - 2:
        return text
    while text and pdf.get_string_width(text + "...") > width - 2:
        text = text[:-1]
    return text + "..."


def build_month_pdf(label, rows, total_hours, total_tasks, completed, in_progress):
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(*BRAND_RGB)
    pdf.cell(0, 10, f"Time Report - {label}", ln=1)

    pdf.set_font("Helvetica", "", 11)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(
        0,
        7,
        f"Total Hours: {total_hours}    Tasks: {total_tasks}    "
        f"Completed: {completed}    In Progress: {in_progress}",
        ln=1,
    )
    pdf.ln(4)

    col_widths = [26, 96, 20, 30]
    headers = ["Date", "Task", "Hours", "Status"]

    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(*BRAND_RGB)
    pdf.set_text_color(255, 255, 255)
    for width, header in zip(col_widths, headers):
        pdf.cell(width, 9, header, border=1, fill=True, align="L")
    pdf.ln()

    pdf.set_font("Helvetica", "", 10)
    for i, row in enumerate(rows):
        pdf.set_fill_color(245, 247, 246) if i % 2 else pdf.set_fill_color(255, 255, 255)
        date_display = datetime.strptime(row["date"], "%Y-%m-%d").strftime("%d/%m/%Y")
        task_display = _fit_text(pdf, row["task"], col_widths[1], 10)

        pdf.set_text_color(30, 30, 30)
        pdf.cell(col_widths[0], 8, date_display, border=1, fill=True)
        pdf.cell(col_widths[1], 8, task_display, border=1, fill=True)
        pdf.cell(col_widths[2], 8, f"{row['hours']:.1f}", border=1, fill=True)

        pdf.set_text_color(*STATUS_RGB.get(row["status"], (30, 30, 30)))
        pdf.cell(col_widths[3], 8, row["status"], border=1, fill=True)
        pdf.ln()

    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(30, 30, 30)
    pdf.set_fill_color(235, 238, 236)
    pdf.cell(col_widths[0] + col_widths[1], 9, "Total", border=1, fill=True)
    pdf.cell(col_widths[2], 9, f"{total_hours:.1f}", border=1, fill=True)
    pdf.cell(col_widths[3], 9, "", border=1, fill=True)

    return bytes(pdf.output())


@app.route("/")
def index():
    db = get_db()
    rows = db.execute("SELECT * FROM entries ORDER BY date ASC").fetchall()

    groups = {}
    for row in rows:
        groups.setdefault(row["date"][:7], []).append(row)

    month_keys = sorted(groups.keys(), reverse=True)
    month_summaries = [
        {
            "key": key,
            "label": datetime.strptime(key, "%Y-%m").strftime("%B %Y"),
            "total_hours": sum(r["hours"] for r in groups[key]),
            "total_tasks": len(groups[key]),
            "completed": sum(1 for r in groups[key] if r["status"] == "Completed"),
            "in_progress": sum(1 for r in groups[key] if r["status"] == "In Progress"),
        }
        for key in month_keys
    ]

    return render_template(
        "index.html",
        month_summaries=month_summaries,
        total_hours=sum(r["hours"] for r in rows),
        completed=sum(1 for r in rows if r["status"] == "Completed"),
        in_progress=sum(1 for r in rows if r["status"] == "In Progress"),
        total_tasks=len(rows),
        statuses=STATUSES,
        today=datetime.now().strftime("%Y-%m-%d"),
    )


@app.route("/month/<key>")
def month_detail(key):
    if not MONTH_KEY_RE.match(key):
        abort(404)
    try:
        label = datetime.strptime(key, "%Y-%m").strftime("%B %Y")
    except ValueError:
        abort(404)

    sort = request.args.get("sort", "date")
    direction = request.args.get("dir", "asc")
    if sort not in SORT_COLUMNS:
        sort = "date"
    if direction not in ("asc", "desc"):
        direction = "asc"

    db = get_db()
    rows = db.execute(
        f"SELECT * FROM entries WHERE date LIKE ? ORDER BY {SORT_COLUMNS[sort]} {direction}",
        (f"{key}-%",),
    ).fetchall()

    def next_dir(col):
        return "desc" if sort == col and direction == "asc" else "asc"

    return render_template(
        "month.html",
        month_key=key,
        month_label=label,
        rows=rows,
        statuses=STATUSES,
        sort=sort,
        direction=direction,
        next_dir=next_dir,
        total_hours=sum(r["hours"] for r in rows),
        completed=sum(1 for r in rows if r["status"] == "Completed"),
        in_progress=sum(1 for r in rows if r["status"] == "In Progress"),
        total_tasks=len(rows),
        today=datetime.now().strftime("%Y-%m-%d"),
    )


@app.route("/month/<key>/download")
def month_download(key):
    if not MONTH_KEY_RE.match(key):
        abort(404)
    try:
        label = datetime.strptime(key, "%Y-%m").strftime("%B %Y")
    except ValueError:
        abort(404)

    db = get_db()
    rows = db.execute(
        "SELECT * FROM entries WHERE date LIKE ? ORDER BY date ASC",
        (f"{key}-%",),
    ).fetchall()

    pdf_bytes = build_month_pdf(
        label,
        rows,
        total_hours=sum(r["hours"] for r in rows),
        total_tasks=len(rows),
        completed=sum(1 for r in rows if r["status"] == "Completed"),
        in_progress=sum(1 for r in rows if r["status"] == "In Progress"),
    )

    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"time-report-{key}.pdf",
    )


def _redirect_next():
    next_url = request.form.get("next")
    return redirect(next_url) if next_url else redirect(url_for("index"))


@app.route("/entries/add", methods=["POST"])
def add_entry():
    db = get_db()
    db.execute(
        "INSERT INTO entries (date, task, hours, status) VALUES (?, ?, ?, ?)",
        (
            request.form["date"],
            request.form["task"],
            float(request.form.get("hours") or 0),
            request.form.get("status", STATUSES[0]),
        ),
    )
    db.commit()
    return _redirect_next()


@app.route("/entries/<int:entry_id>/update", methods=["POST"])
def update_entry(entry_id):
    db = get_db()
    db.execute(
        "UPDATE entries SET date = ?, task = ?, hours = ?, status = ? WHERE id = ?",
        (
            request.form["date"],
            request.form["task"],
            float(request.form.get("hours") or 0),
            request.form.get("status", STATUSES[0]),
            entry_id,
        ),
    )
    db.commit()
    return _redirect_next()


@app.route("/entries/<int:entry_id>/delete", methods=["POST"])
def delete_entry(entry_id):
    db = get_db()
    db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    db.commit()
    return _redirect_next()


init_db()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001)
