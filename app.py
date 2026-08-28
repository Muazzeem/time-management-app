import os
import re
import sqlite3
from datetime import datetime
from io import BytesIO
from pathlib import Path

from fpdf import FPDF
from flask import Flask, abort, g, redirect, render_template, request, send_file, url_for

app = Flask(__name__)

DB_PATH = Path(__file__).parent / "time_log.db"
FONT_DIR = Path(__file__).parent / "fonts"

# Render's local disk is wiped on every restart/redeploy, so SQLite alone
# doesn't survive there. When TURSO_DATABASE_URL is set (e.g. on Render),
# every read/write goes straight to a persistent Turso database over HTTP.
# Without it (local development), we fall back to a plain local SQLite file
# so `python app.py` keeps working with zero setup.
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

if TURSO_DATABASE_URL:
    import turso_serverless

STATUSES = ["Not Started", "In Progress", "Completed", "Blocked"]
SORT_COLUMNS = {"date": "date", "task": "task", "hours": "hours", "status": "status"}
MONTH_KEY_RE = re.compile(r"^\d{4}-\d{2}$")
DEFAULT_HOURLY_RATE = 300.0
DEFAULT_PROJECT_NAME = "General"
CURRENCY_SYMBOL = "৳"

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


def create_connection():
    if TURSO_DATABASE_URL:
        conn = turso_serverless.connect(TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
        conn.row_factory = turso_serverless.Row
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = create_connection()
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = create_connection()

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
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
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            hourly_rate REAL NOT NULL DEFAULT 300
        )
        """
    )

    # Migrate entries tables that predate the project_id column.
    columns = {row["name"] for row in db.execute("PRAGMA table_info(entries)")}
    if "project_id" not in columns:
        db.execute(
            "ALTER TABLE entries ADD COLUMN project_id "
            "INTEGER REFERENCES projects(id) ON DELETE CASCADE"
        )

    if db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0:
        cur = db.execute("INSERT INTO projects (name) VALUES (?)", (DEFAULT_PROJECT_NAME,))
        default_project_id = cur.lastrowid
    else:
        default_project_id = db.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()[0]

    db.execute(
        "UPDATE entries SET project_id = ? WHERE project_id IS NULL",
        (default_project_id,),
    )

    if db.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0:
        db.executemany(
            "INSERT INTO entries (project_id, date, task, hours, status) VALUES (?, ?, ?, ?, ?)",
            [(default_project_id, *entry) for entry in SEED_ENTRIES],
        )

    if db.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0:
        db.execute(
            "INSERT INTO settings (id, hourly_rate) VALUES (1, ?)",
            (DEFAULT_HOURLY_RATE,),
        )

    db.commit()
    db.close()


def get_project_or_404(project_id):
    db = get_db()
    project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        abort(404)
    return project


def get_hourly_rate():
    db = get_db()
    row = db.execute("SELECT hourly_rate FROM settings WHERE id = 1").fetchone()
    return row["hourly_rate"] if row else DEFAULT_HOURLY_RATE


def _slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "project"


@app.template_filter("currency")
def currency_filter(value):
    try:
        return f"{CURRENCY_SYMBOL}{float(value):,.2f}"
    except (TypeError, ValueError):
        return value


@app.context_processor
def inject_hourly_rate():
    return {"hourly_rate": get_hourly_rate()}


def _fit_text(pdf, text, width, font_size):
    pdf.set_font("NotoBengali", "", font_size)
    if pdf.get_string_width(text) <= width - 2:
        return text
    while text and pdf.get_string_width(text + "...") > width - 2:
        text = text[:-1]
    return text + "..."


def build_month_pdf(label, rows, total_hours, total_tasks, completed, in_progress, rate):
    total_amount = total_hours * rate

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_font("NotoBengali", "", str(FONT_DIR / "NotoSansBengali-Regular.ttf"))
    pdf.add_font("NotoBengali", "B", str(FONT_DIR / "NotoSansBengali-Bold.ttf"))
    pdf.add_page()

    pdf.set_font("NotoBengali", "B", 18)
    pdf.set_text_color(*BRAND_RGB)
    pdf.cell(0, 10, f"Time Report - {label}", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("NotoBengali", "", 11)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(
        0,
        7,
        f"Total Hours: {total_hours}    Tasks: {total_tasks}    "
        f"Completed: {completed}    In Progress: {in_progress}",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.set_font("NotoBengali", "B", 13)
    pdf.set_text_color(*BRAND_RGB)
    pdf.cell(
        0,
        9,
        f"Total Amount Earned ({CURRENCY_SYMBOL}{rate:,.2f}/hr): {CURRENCY_SYMBOL}{total_amount:,.2f}",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.ln(2)

    col_widths = [28, 108, 22, 32]
    headers = ["Date", "Task", "Hours", "Status"]

    pdf.set_font("NotoBengali", "B", 10)
    pdf.set_fill_color(*BRAND_RGB)
    pdf.set_text_color(255, 255, 255)
    for width, header in zip(col_widths, headers):
        pdf.cell(width, 9, header, border=1, fill=True, align="L")
    pdf.ln()

    pdf.set_font("NotoBengali", "", 10)
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

    pdf.set_font("NotoBengali", "B", 10)
    pdf.set_text_color(30, 30, 30)
    pdf.set_fill_color(235, 238, 236)
    pdf.cell(col_widths[0] + col_widths[1], 9, "Total", border=1, fill=True)
    pdf.cell(col_widths[2], 9, f"{total_hours:.1f}", border=1, fill=True)
    pdf.cell(col_widths[3], 9, "", border=1, fill=True)

    return bytes(pdf.output())


@app.route("/")
def index():
    db = get_db()
    projects = db.execute(
        """
        SELECT p.id, p.name,
               COALESCE(SUM(e.hours), 0) AS total_hours,
               COUNT(e.id) AS total_tasks,
               SUM(CASE WHEN e.status = 'Completed' THEN 1 ELSE 0 END) AS completed,
               SUM(CASE WHEN e.status = 'In Progress' THEN 1 ELSE 0 END) AS in_progress
        FROM projects p
        LEFT JOIN entries e ON e.project_id = p.id
        GROUP BY p.id
        ORDER BY p.id DESC
        """
    ).fetchall()
    return render_template("projects.html", projects=projects)


@app.route("/projects/add", methods=["POST"])
def add_project():
    name = (request.form.get("name") or "").strip()
    if name:
        db = get_db()
        db.execute("INSERT INTO projects (name) VALUES (?)", (name,))
        db.commit()
    return redirect(url_for("index"))


@app.route("/projects/<int:project_id>/rename", methods=["POST"])
def rename_project(project_id):
    get_project_or_404(project_id)
    name = (request.form.get("name") or "").strip()
    if name:
        db = get_db()
        db.execute("UPDATE projects SET name = ? WHERE id = ?", (name, project_id))
        db.commit()
    return redirect(url_for("index"))


@app.route("/projects/<int:project_id>/delete", methods=["POST"])
def delete_project(project_id):
    get_project_or_404(project_id)
    db = get_db()
    db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    db.commit()
    return redirect(url_for("index"))


@app.route("/projects/<int:project_id>")
def project_dashboard(project_id):
    project = get_project_or_404(project_id)
    db = get_db()
    rows = db.execute(
        "SELECT * FROM entries WHERE project_id = ? ORDER BY date ASC", (project_id,)
    ).fetchall()
    rate = get_hourly_rate()

    groups = {}
    for row in rows:
        groups.setdefault(row["date"][:7], []).append(row)

    month_keys = sorted(groups.keys(), reverse=True)
    month_summaries = [
        {
            "key": key,
            "label": datetime.strptime(key, "%Y-%m").strftime("%B %Y"),
            "total_hours": sum(r["hours"] for r in groups[key]),
            "amount": sum(r["hours"] for r in groups[key]) * rate,
            "total_tasks": len(groups[key]),
            "completed": sum(1 for r in groups[key] if r["status"] == "Completed"),
            "in_progress": sum(1 for r in groups[key] if r["status"] == "In Progress"),
        }
        for key in month_keys
    ]

    total_hours = sum(r["hours"] for r in rows)
    return render_template(
        "project.html",
        project=project,
        month_summaries=month_summaries,
        total_hours=total_hours,
        total_amount=total_hours * rate,
        completed=sum(1 for r in rows if r["status"] == "Completed"),
        in_progress=sum(1 for r in rows if r["status"] == "In Progress"),
        total_tasks=len(rows),
        statuses=STATUSES,
        today=datetime.now().strftime("%Y-%m-%d"),
    )


@app.route("/projects/<int:project_id>/month/<key>")
def month_detail(project_id, key):
    project = get_project_or_404(project_id)
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
        f"SELECT * FROM entries WHERE project_id = ? AND date LIKE ? "
        f"ORDER BY {SORT_COLUMNS[sort]} {direction}",
        (project_id, f"{key}-%"),
    ).fetchall()

    def next_dir(col):
        return "desc" if sort == col and direction == "asc" else "asc"

    total_hours = sum(r["hours"] for r in rows)
    return render_template(
        "month.html",
        project=project,
        month_key=key,
        month_label=label,
        rows=rows,
        statuses=STATUSES,
        sort=sort,
        direction=direction,
        next_dir=next_dir,
        total_hours=total_hours,
        total_amount=total_hours * get_hourly_rate(),
        completed=sum(1 for r in rows if r["status"] == "Completed"),
        in_progress=sum(1 for r in rows if r["status"] == "In Progress"),
        total_tasks=len(rows),
        today=datetime.now().strftime("%Y-%m-%d"),
    )


@app.route("/projects/<int:project_id>/month/<key>/download")
def month_download(project_id, key):
    project = get_project_or_404(project_id)
    if not MONTH_KEY_RE.match(key):
        abort(404)
    try:
        label = datetime.strptime(key, "%Y-%m").strftime("%B %Y")
    except ValueError:
        abort(404)

    db = get_db()
    rows = db.execute(
        "SELECT * FROM entries WHERE project_id = ? AND date LIKE ? ORDER BY date ASC",
        (project_id, f"{key}-%"),
    ).fetchall()

    pdf_bytes = build_month_pdf(
        f"{project['name']} - {label}",
        rows,
        total_hours=sum(r["hours"] for r in rows),
        total_tasks=len(rows),
        completed=sum(1 for r in rows if r["status"] == "Completed"),
        in_progress=sum(1 for r in rows if r["status"] == "In Progress"),
        rate=get_hourly_rate(),
    )

    slug = _slugify(project["name"])
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"time-report-{slug}-{key}.pdf",
    )


def _redirect_next():
    next_url = request.form.get("next")
    return redirect(next_url) if next_url else redirect(url_for("index"))


@app.route("/settings/rate", methods=["POST"])
def update_rate():
    rate = max(0.0, float(request.form.get("rate") or 0))
    db = get_db()
    db.execute("UPDATE settings SET hourly_rate = ? WHERE id = 1", (rate,))
    db.commit()
    return _redirect_next()


@app.route("/projects/<int:project_id>/entries/add", methods=["POST"])
def add_entry(project_id):
    get_project_or_404(project_id)
    db = get_db()
    db.execute(
        "INSERT INTO entries (project_id, date, task, hours, status) VALUES (?, ?, ?, ?, ?)",
        (
            project_id,
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
