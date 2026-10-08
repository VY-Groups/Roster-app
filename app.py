from __future__ import annotations

import csv
import calendar
import io
import os
import secrets
import sqlite3
import tempfile
from itertools import combinations
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from flask import Flask, current_app, g, has_request_context, jsonify, redirect, render_template, request, send_file, session, url_for
from flask.testing import FlaskClient
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.security import check_password_hash, generate_password_hash

MIN_ACTIVE_EMPLOYEES = 3
MIN_SATURDAYS_OFF = 2


class RosterTestClient(FlaskClient):
    def open(self, *args: Any, **kwargs: Any):
        method = str(kwargs.get("method", "GET")).upper()
        if method not in {"GET", "HEAD", "OPTIONS"}:
            headers = dict(kwargs.get("headers") or {})
            if "X-CSRF-Token" not in headers:
                with self.session_transaction() as current_session:
                    token = current_session.get("csrf_token")
                if token:
                    headers["X-CSRF-Token"] = token
                    kwargs["headers"] = headers
        return super().open(*args, **kwargs)


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        DATABASE=os.environ.get(
            "ROSTER_DATABASE", str(Path(app.instance_path) / "roster.db")
        ),
        SECRET_KEY=os.environ.get("ROSTER_SECRET_KEY"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("ROSTER_COOKIE_SECURE") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=64 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    if app.config.get("TESTING"):
        app.test_client_class = RosterTestClient

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    if not app.config.get("SECRET_KEY"):
        secret_path = Path(app.instance_path) / ".session-secret"
        try:
            secret_fd = os.open(
                secret_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            pass
        else:
            with os.fdopen(secret_fd, "w", encoding="ascii") as secret_file:
                secret_file.write(secrets.token_hex(32))
        app.config["SECRET_KEY"] = secret_path.read_text(encoding="ascii").strip()

    @app.teardown_appcontext
    def close_db(_error: BaseException | None = None) -> None:
        db = g.pop("db", None)
        if db is not None:
            db.close()

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    with app.app_context():
        get_db().executescript(
            """
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'operator', 'viewer')),
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS leave_days (
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                date TEXT NOT NULL,
                PRIMARY KEY (employee_id, date)
            );
            CREATE TABLE IF NOT EXISTS leave_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS assignments (
                date TEXT PRIMARY KEY,
                employee1_id INTEGER NOT NULL REFERENCES employees(id),
                employee2_id INTEGER NOT NULL REFERENCES employees(id),
                manual INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS attendance_records (
                date TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS attendance_employees (
                date TEXT NOT NULL REFERENCES attendance_records(date) ON DELETE CASCADE,
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                PRIMARY KEY (date, employee_id)
            );
            CREATE TABLE IF NOT EXISTS employee_unavailable_weekdays (
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
                PRIMARY KEY (employee_id, weekday)
            );
            CREATE TABLE IF NOT EXISTS employee_pair_exclusions (
                employee1_id INTEGER NOT NULL REFERENCES employees(id),
                employee2_id INTEGER NOT NULL REFERENCES employees(id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (employee1_id < employee2_id),
                PRIMARY KEY (employee1_id, employee2_id)
            );
            CREATE TABLE IF NOT EXISTS holidays (
                date TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                dc_closed INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                actor TEXT NOT NULL DEFAULT 'Local operator',
                action TEXT NOT NULL,
                employee_name TEXT NOT NULL DEFAULT '',
                date TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL DEFAULT ''
            );
            """
        )

    @app.before_request
    def require_account_and_protect_mutations():
        if request.endpoint in {"static", "setup", "create_initial_admin", "login"}:
            return None

        db = get_db()
        active_user = None
        user_id = session.get("user_id")
        if user_id is not None:
            active_user = db.execute(
                "SELECT id, username, role FROM users WHERE id = ? AND active = 1",
                (user_id,),
            ).fetchone()
            if active_user is None:
                session.clear()
        g.user = dict(active_user) if active_user is not None else None

        user_count = db.execute(
            "SELECT COUNT(*) FROM users WHERE active = 1"
        ).fetchone()[0]
        if user_count == 0 and request.endpoint != "setup":
            if request.path.startswith("/api/"):
                return api_error("Complete initial administrator setup.", 428)
            return redirect(url_for("setup"))
        if g.user is None:
            if request.path.startswith("/api/"):
                return api_error("Sign in to use the roster.", 401)
            return redirect(url_for("login", next=request.path))

        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            csrf_token = session.get("csrf_token")
            supplied_token = (
                request.headers.get("X-CSRF-Token")
                or request.form.get("csrf_token")
            )
            if not csrf_token or not secrets.compare_digest(
                str(csrf_token), supplied_token or ""
            ):
                return api_error("The request security token is missing or invalid.", 400)
            if g.user["role"] == "viewer":
                return api_error("Viewer accounts cannot make changes.", 403)
            if request.path.startswith(("/api/users", "/api/backup", "/api/restore")):
                if g.user["role"] != "admin":
                    return api_error("Administrator access is required.", 403)
        if request.path.startswith(("/api/users", "/api/backup", "/api/restore")):
            if g.user["role"] != "admin":
                return api_error("Administrator access is required.", 403)
        return None

    @app.get("/setup")
    def setup():
        db = get_db()
        if db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None:
            return redirect(url_for("login"))
        token = session.setdefault("csrf_token", secrets.token_urlsafe(32))
        return render_template("auth.html", mode="setup", csrf_token=token, error="")

    @app.post("/setup")
    def create_initial_admin():
        db = get_db()
        if db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None:
            return redirect(url_for("login"))
        if not secrets.compare_digest(
            session.get("csrf_token", ""), request.form.get("csrf_token", "")
        ):
            return api_error("The request security token is missing or invalid.", 400)
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or len(username) > 80:
            return render_template(
                "auth.html", mode="setup", csrf_token=session["csrf_token"],
                error="Username must contain 1 to 80 characters.",
            ), 400
        if len(password) < 12:
            return render_template(
                "auth.html", mode="setup", csrf_token=session["csrf_token"],
                error="Use a password with at least 12 characters.",
            ), 400
        try:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None:
                db.rollback()
                return redirect(url_for("login"))
            cursor = db.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
                (username, generate_password_hash(password)),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            return render_template(
                "auth.html", mode="setup", csrf_token=session["csrf_token"],
                error="That username is already in use.",
            ), 409
        session.clear()
        session.permanent = True
        session["user_id"] = cursor.lastrowid
        session["csrf_token"] = secrets.token_urlsafe(32)
        write_activity(
            db,
            "Administrator account created",
            actor=username,
            details="Initial administrator account established.",
        )
        return redirect(url_for("index"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        token = session.setdefault("csrf_token", secrets.token_urlsafe(32))
        if request.method == "GET":
            if session.get("user_id"):
                return redirect(url_for("index"))
            return render_template("auth.html", mode="login", csrf_token=token, error="")
        if not secrets.compare_digest(
            session.get("csrf_token", ""), request.form.get("csrf_token", "")
        ):
            return api_error("The request security token is missing or invalid.", 400)
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute(
            "SELECT id, password_hash FROM users WHERE username = ? AND active = 1",
            (username,),
        ).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            return render_template(
                "auth.html", mode="login", csrf_token=token,
                error="Username or password is incorrect.",
            ), 401
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        session["csrf_token"] = secrets.token_urlsafe(32)
        return redirect(url_for("index"))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            current_user=g.user,
            csrf_token=session["csrf_token"],
        )

    @app.get("/api/users")
    def list_users():
        rows = get_db().execute(
            "SELECT id, username, role, active, created_at FROM users ORDER BY username COLLATE NOCASE"
        ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.post("/api/users")
    def add_user():
        payload = request.get_json(silent=True) or {}
        username = payload.get("username")
        password = payload.get("password")
        role = payload.get("role")
        if not isinstance(username, str) or not username.strip() or len(username.strip()) > 80:
            return api_error("Username must contain 1 to 80 characters.", 400)
        if not isinstance(password, str) or len(password) < 12:
            return api_error("Use a password with at least 12 characters.", 400)
        if not isinstance(role, str) or role not in {"admin", "operator", "viewer"}:
            return api_error("Choose admin, operator, or viewer role.", 400)
        try:
            cursor = get_db().execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                (username.strip(), generate_password_hash(password), role),
            )
            get_db().commit()
        except sqlite3.IntegrityError:
            return api_error("That username is already in use.", 409)
        write_activity(
            get_db(),
            "Account created",
            details=f"Created {role} account '{username.strip()}'.",
        )
        return jsonify(
            {
                "id": cursor.lastrowid,
                "username": username.strip(),
                "role": role,
                "active": True,
            }
        ), 201

    @app.patch("/api/users/<int:user_id>")
    def update_user(user_id: int):
        payload = request.get_json(silent=True) or {}
        role = payload.get("role")
        active = payload.get("active")
        password = payload.get("password")
        if role is not None and (
            not isinstance(role, str) or role not in {"admin", "operator", "viewer"}
        ):
            return api_error("Choose admin, operator, or viewer role.", 400)
        if active is not None and not isinstance(active, bool):
            return api_error("Active must be true or false.", 400)
        if password is not None and (
            not isinstance(password, str) or len(password) < 12
        ):
            return api_error("Use a password with at least 12 characters.", 400)
        db = get_db()
        user = db.execute(
            "SELECT id, username, role, active FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if user is None:
            return api_error("That account was not found.", 404)
        new_role = role or user["role"]
        new_active = user["active"] if active is None else int(active)
        if user_id == g.user["id"] and not new_active:
            return api_error("You cannot deactivate your own account.", 409)
        if user_id == g.user["id"] and new_role != "admin":
            return api_error("You cannot change your own administrator role.", 409)
        if user["role"] == "admin" and user["active"] and (
            new_role != "admin" or not new_active
        ):
            other_admins = db.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1 AND id != ?",
                (user_id,),
            ).fetchone()[0]
            if other_admins == 0:
                return api_error("At least one active administrator must remain.", 409)
        updates = ["role = ?", "active = ?"]
        values: list[Any] = [new_role, new_active]
        if password is not None:
            updates.append("password_hash = ?")
            values.append(generate_password_hash(password))
        values.append(user_id)
        db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", values)
        db.commit()
        write_activity(
            db,
            "Account updated",
            details=f"Updated account '{user['username']}' (role: {new_role}; active: {bool(new_active)}).",
        )
        return jsonify(
            {
                "id": user_id,
                "username": user["username"],
                "role": new_role,
                "active": bool(new_active),
            }
        )

    @app.get("/api/backup")
    def download_backup():
        db_path = Path(current_app.config["DATABASE"])
        if str(db_path) == ":memory:":
            return api_error("Backups require a file-based SQLite database.", 409)
        backup_directory = tempfile.TemporaryDirectory(prefix="roster-backup-")
        backup_path = Path(backup_directory.name) / "roster-backup.db"
        destination = sqlite3.connect(backup_path)
        try:
            get_db().backup(destination)
        except sqlite3.Error:
            destination.close()
            backup_directory.cleanup()
            raise
        destination.close()
        if backup_path.stat().st_size > current_app.config["MAX_CONTENT_LENGTH"]:
            backup_directory.cleanup()
            return api_error("Backups must be 64 MB or smaller.", 413)
        backup_data = backup_path.read_bytes()
        backup_directory.cleanup()
        response = send_file(
            io.BytesIO(backup_data),
            mimetype="application/vnd.sqlite3",
            as_attachment=True,
            download_name=f"roster-backup-{date.today().isoformat()}.db",
        )
        return response

    @app.post("/api/restore")
    def restore_backup():
        if request.form.get("confirm_replace") != "true":
            return api_error("Confirm that the current roster should be replaced.", 400)
        uploaded = request.files.get("backup")
        if uploaded is None or not uploaded.filename:
            return api_error("Choose a SQLite backup file to restore.", 400)

        db_path = Path(current_app.config["DATABASE"])
        if str(db_path) == ":memory:":
            return api_error("Restore requires a file-based SQLite database.", 409)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        required_schema = {
            "users": {"id", "username", "password_hash", "role", "active", "created_at"},
            "employees": {"id", "name", "active"},
            "employee_pair_exclusions": {
                "employee1_id", "employee2_id", "created_at"
            },
            "leave_days": {"employee_id", "date"},
            "leave_requests": {
                "id", "employee_id", "start_date", "end_date", "status", "created_at"
            },
            "assignments": {
                "date", "employee1_id", "employee2_id", "manual"
            },
            "attendance_records": {"date"},
            "attendance_employees": {"date", "employee_id"},
            "employee_unavailable_weekdays": {"employee_id", "weekday"},
            "holidays": {"date", "name", "dc_closed"},
        }
        safety_backup: Path | None = None
        with tempfile.TemporaryDirectory(
            prefix=".roster-restore-", dir=db_path.parent
        ) as staging_directory:
            staged_path = Path(staging_directory) / "uploaded-backup.db"
            uploaded.save(staged_path)
            try:
                validation_db = sqlite3.connect(staged_path)
                try:
                    integrity = validation_db.execute("PRAGMA integrity_check").fetchone()
                    if integrity is None or integrity[0] != "ok":
                        return api_error("The uploaded backup is not a valid SQLite database.", 400)
                    tables = {
                        row[0]
                        for row in validation_db.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        ).fetchall()
                    }
                    for table, required_columns in required_schema.items():
                        if table not in tables:
                            return api_error(
                                "The uploaded file is not a compatible roster backup.",
                                400,
                            )
                        columns = {
                            row[1]
                            for row in validation_db.execute(
                                f'PRAGMA table_info("{table}")'
                            ).fetchall()
                        }
                        if not required_columns <= columns:
                            return api_error(
                                "The uploaded file is not a compatible roster backup.",
                                400,
                            )
                finally:
                    validation_db.close()
            except sqlite3.Error:
                return api_error("The uploaded file is not a valid SQLite roster backup.", 400)

            if not db_path.is_file():
                return api_error("The current roster database could not be found.", 409)
            current_db = get_db()
            current_db.commit()
            backup_handle, backup_name = tempfile.mkstemp(
                prefix=f"{db_path.stem}-pre-restore-",
                suffix=".db",
                dir=db_path.parent,
            )
            os.close(backup_handle)
            safety_backup = Path(backup_name)
            safety_db = sqlite3.connect(safety_backup)
            try:
                current_db.backup(safety_db)
            except sqlite3.Error:
                safety_db.close()
                safety_backup.unlink(missing_ok=True)
                raise
            safety_db.close()
            g.pop("db", None)
            current_db.close()
            try:
                os.replace(staged_path, db_path)
            except OSError:
                app.logger.exception("Failed to replace roster database during restore")
                return api_error(
                    "Restore failed. The existing database was preserved; "
                    f"safety backup: {safety_backup.name}.",
                    500,
                )
        write_activity(
            get_db(),
            "Database restored",
            details=f"Previous database preserved as {safety_backup.name}.",
        )
        return jsonify(
            {
                "ok": True,
                "safety_backup": safety_backup.name if safety_backup else None,
            }
        )

    @app.get("/api/employees/export.csv")
    def export_employees_csv():
        rows = get_db().execute(
            """
            SELECT id, name, active
            FROM employees
            ORDER BY active DESC, name COLLATE NOCASE
            """
        ).fetchall()
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["ID", "Name", "Active"])
        writer.writerows(
            [row["id"], row["name"], "Yes" if row["active"] else "No"]
            for row in rows
        )
        return send_file(
            io.BytesIO(output.getvalue().encode("utf-8-sig")),
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"roster-employees-{date.today().isoformat()}.csv",
        )

    @app.get("/api/employees")
    def list_employees():
        employees = get_db().execute(
            "SELECT id, name FROM employees WHERE active = 1 ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return jsonify([dict(employee) for employee in employees])

    @app.get("/api/activity")
    def recent_activity():
        search = request.args.get("search", "").strip()
        date_filter = request.args.get("date", "").strip()
        if len(search) > 200:
            return api_error("Activity search must be 200 characters or fewer.", 400)
        if date_filter:
            try:
                parsed_date = date.fromisoformat(date_filter)
            except ValueError:
                return api_error("Provide a valid activity date.", 400)
            if parsed_date.isoformat() != date_filter:
                return api_error("Provide a valid activity date.", 400)

        try:
            page = int(request.args.get("page", "1"))
            page_size = int(request.args.get("page_size", "20"))
        except ValueError:
            return api_error("Page and page size must be whole numbers.", 400)
        if page < 1:
            return api_error("Page must be at least 1.", 400)
        if not 1 <= page_size <= 100:
            return api_error("Page size must be between 1 and 100.", 400)

        conditions = []
        parameters: list[str | int] = []
        if search:
            escaped_search = (
                search.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            match = f"%{escaped_search}%"
            conditions.append(
                """
                (actor LIKE ? ESCAPE '\\'
                 OR action LIKE ? ESCAPE '\\'
                 OR employee_name LIKE ? ESCAPE '\\'
                 OR date LIKE ? ESCAPE '\\'
                 OR details LIKE ? ESCAPE '\\')
                """
            )
            parameters.extend([match] * 5)
        if date_filter:
            conditions.append("(date = ? OR substr(occurred_at, 1, 10) = ?)")
            parameters.extend([date_filter, date_filter])
        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        db = get_db()
        total = db.execute(
            f"SELECT COUNT(*) FROM activity_log{where_clause}",
            parameters,
        ).fetchone()[0]
        total_pages = (total + page_size - 1) // page_size
        page = min(page, max(total_pages, 1))
        rows = db.execute(
            f"""
            SELECT id, occurred_at, actor, action, employee_name, date, details
            FROM activity_log
            {where_clause}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            [*parameters, page_size, (page - 1) * page_size],
        ).fetchall()
        return jsonify(
            {
                "entries": [dict(row) for row in rows],
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
            }
        )

    @app.get("/api/activity.csv")
    def export_activity_csv():
        rows = get_db().execute(
            """
            SELECT id, occurred_at, actor, action, employee_name, date, details
            FROM activity_log
            ORDER BY id
            """
        ).fetchall()
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            ["ID", "Timestamp", "Operator", "Action", "Employee", "Date", "Details"]
        )
        writer.writerows(
            [
                row["id"],
                row["occurred_at"],
                row["actor"],
                row["action"],
                row["employee_name"],
                row["date"],
                row["details"],
            ]
            for row in rows
        )
        return send_file(
            io.BytesIO(output.getvalue().encode("utf-8-sig")),
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"roster-activity-{date.today().isoformat()}.csv",
        )

    @app.get("/api/employees/template.csv")
    def employee_import_template():
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["Name"])
        writer.writerow(["Avery"])
        return send_file(
            io.BytesIO(output.getvalue().encode("utf-8-sig")),
            mimetype="text/csv",
            as_attachment=True,
            download_name="employee-import-template.csv",
        )

    @app.post("/api/employees/import")
    def import_employees():
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return api_error("Choose a CSV file to import.", 400)
        if Path(uploaded.filename).suffix.lower() != ".csv":
            return api_error("Employee imports must be CSV files.", 400)
        try:
            content = uploaded.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            return api_error("The CSV file must use UTF-8 encoding.", 400)

        try:
            rows = list(csv.reader(io.StringIO(content, newline=""), strict=True))
        except csv.Error:
            return api_error("The CSV file is malformed.", 400)
        has_header = bool(
            rows and rows[0] and rows[0][0].strip().casefold() == "name"
        )
        if has_header:
            rows = rows[1:]

        names: list[str] = []
        seen_names: set[str] = set()
        duplicate_names: list[str] = []
        row_number_start = 2 if has_header else 1
        processed_rows = 0
        for row_number, row in enumerate(rows, start=row_number_start):
            if not row or all(not value.strip() for value in row):
                continue
            processed_rows += 1
            if processed_rows > 500:
                return api_error("A single import cannot exceed 500 employee rows.", 400)
            if len(row) != 1:
                return api_error(
                    f"Row {row_number} must contain exactly one employee name.",
                    400,
                )
            name = row[0].strip()
            if not name:
                continue
            if len(name) > 80:
                return api_error(
                    f"Employee name on row {row_number} exceeds 80 characters.",
                    400,
                )
            normalized = name.casefold()
            if normalized in seen_names:
                duplicate_names.append(name)
                continue
            names.append(name)
            seen_names.add(normalized)
        if not names:
            return api_error("The CSV file contains no employee names.", 400)

        db = get_db()
        added: list[str] = []
        reactivated: list[str] = []
        skipped = duplicate_names
        try:
            db.execute("BEGIN")
            for name in names:
                employee = db.execute(
                    "SELECT id, name, active FROM employees WHERE name = ?",
                    (name,),
                ).fetchone()
                if employee is None:
                    db.execute("INSERT INTO employees (name) VALUES (?)", (name,))
                    added.append(name)
                elif employee["active"]:
                    skipped.append(employee["name"])
                else:
                    db.execute(
                        "UPDATE employees SET active = 1 WHERE id = ?",
                        (employee["id"],),
                    )
                    reactivated.append(employee["name"])
            db.commit()
        except sqlite3.Error:
            db.rollback()
            raise

        write_activity(
            db,
            "Employees imported",
            details=(
                f"Added {len(added)}, reactivated {len(reactivated)}, "
                f"skipped {len(skipped)} existing active employee(s)."
            ),
        )
        if added or reactivated:
            rebalance_scheduled_future()
        return jsonify(
            {
                "added": added,
                "reactivated": reactivated,
                "skipped": skipped,
            }
        )

    @app.get("/api/leave/template.csv")
    def leave_import_template():
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["Employee", "Date"])
        writer.writerow(["Avery", "2026-10-10"])
        return send_file(
            io.BytesIO(output.getvalue().encode("utf-8-sig")),
            mimetype="text/csv",
            as_attachment=True,
            download_name="leave-import-template.csv",
        )

    @app.post("/api/leave/import")
    def import_leave():
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return api_error("Choose a CSV file to import.", 400)
        if Path(uploaded.filename).suffix.lower() != ".csv":
            return api_error("Leave imports must be CSV files.", 400)
        try:
            content = uploaded.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            return api_error("The CSV file must use UTF-8 encoding.", 400)
        try:
            rows = list(csv.reader(io.StringIO(content, newline=""), strict=True))
        except csv.Error:
            return api_error("The CSV file is malformed.", 400)

        has_header = bool(
            rows
            and len(rows[0]) == 2
            and [value.strip().casefold() for value in rows[0]]
            == ["employee", "date"]
        )
        if has_header:
            rows = rows[1:]

        parsed_rows: list[tuple[int, str, date]] = []
        row_number_start = 2 if has_header else 1
        processed_rows = 0
        for row_number, row in enumerate(rows, start=row_number_start):
            if not row or all(not value.strip() for value in row):
                continue
            processed_rows += 1
            if processed_rows > 500:
                return api_error("A single import cannot exceed 500 leave rows.", 400)
            if len(row) != 2:
                return api_error(
                    f"Row {row_number} must contain an employee name and date.",
                    400,
                )
            employee_name = row[0].strip()
            if not employee_name or len(employee_name) > 80:
                return api_error(
                    f"Employee name on row {row_number} must be 1 to 80 characters.",
                    400,
                )
            date_text = row[1].strip()
            try:
                leave_date = date.fromisoformat(date_text)
            except ValueError:
                return api_error(
                    f"Date on row {row_number} must use YYYY-MM-DD format.",
                    400,
                )
            if date_text != leave_date.isoformat():
                return api_error(
                    f"Date on row {row_number} must use YYYY-MM-DD format.",
                    400,
                )
            parsed_rows.append((row_number, employee_name, leave_date))
        if not parsed_rows:
            return api_error("The CSV file contains no leave rows.", 400)

        db = get_db()
        active_count = db.execute(
            "SELECT COUNT(*) FROM employees WHERE active = 1"
        ).fetchone()[0]
        if active_count < MIN_ACTIVE_EMPLOYEES:
            return api_error(
                "At least three employees must be active before importing leave.",
                409,
            )
        active_employees = {
            employee["name"].casefold(): employee
            for employee in db.execute(
                "SELECT id, name FROM employees WHERE active = 1"
            ).fetchall()
        }
        imported_pairs: set[tuple[int, str]] = set()
        additions: list[tuple[int, str, str]] = []
        skipped: list[str] = []
        for row_number, employee_name, leave_date in parsed_rows:
            employee = active_employees.get(employee_name.casefold())
            if employee is None:
                return api_error(
                    f"Active employee '{employee_name}' on row {row_number} was not found.",
                    400,
                )
            leave_date_text = leave_date.isoformat()
            pair = (employee["id"], leave_date_text)
            if pair in imported_pairs:
                skipped.append(f"{employee['name']} - {leave_date_text}")
                continue
            imported_pairs.add(pair)
            pending_request = db.execute(
                """
                SELECT 1 FROM leave_requests
                WHERE employee_id = ? AND status = 'pending'
                  AND start_date <= ? AND end_date >= ?
                LIMIT 1
                """,
                (employee["id"], leave_date_text, leave_date_text),
            ).fetchone()
            if pending_request is not None:
                return api_error(
                    f"Row {row_number} overlaps a pending leave request for "
                    f"{employee['name']} on {leave_date_text}.",
                    409,
                )
            existing_leave = db.execute(
                "SELECT 1 FROM leave_days WHERE employee_id = ? AND date = ?",
                pair,
            ).fetchone()
            if existing_leave is not None:
                skipped.append(f"{employee['name']} - {leave_date_text}")
                continue
            additions.append((employee["id"], leave_date_text, employee["name"]))

        if additions:
            try:
                db.execute("BEGIN")
                db.executemany(
                    "INSERT INTO leave_days (employee_id, date) VALUES (?, ?)",
                    [
                        (employee_id, leave_date)
                        for employee_id, leave_date, _ in additions
                    ],
                )
                write_activity(
                    db,
                    "Leave imported",
                    date=min(leave_date for _, leave_date, _ in additions),
                    details=(
                        f"Added {len(additions)} leave day(s), "
                        f"skipped {len(skipped)} duplicate or existing row(s)."
                    ),
                )
            except sqlite3.Error:
                db.rollback()
                raise
            rebalance_scheduled_future(
                min(date.fromisoformat(leave_date) for _, leave_date, _ in additions)
            )
        return jsonify(
            {
                "added": [
                    f"{employee_name} - {leave_date}"
                    for _, leave_date, employee_name in additions
                ],
                "skipped": skipped,
            }
        )

    @app.post("/api/employees")
    def add_employee():
        payload = request.get_json(silent=True) or {}
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            return api_error("Enter an employee name.", 400)
        if len(name.strip()) > 80:
            return api_error("Employee names must be 80 characters or fewer.", 400)
        try:
            cursor = get_db().execute(
                "INSERT INTO employees (name) VALUES (?)", (name.strip(),)
            )
            get_db().commit()
        except sqlite3.IntegrityError:
            employee = get_db().execute(
                "SELECT id, name, active FROM employees WHERE name = ?",
                (name.strip(),),
            ).fetchone()
            if employee is None or employee["active"]:
                return api_error("That employee already exists.", 409)
            get_db().execute(
                "UPDATE employees SET active = 1 WHERE id = ?", (employee["id"],)
            )
            get_db().commit()
            write_activity(
                get_db(),
                "Employee reactivated",
                employee_name=employee["name"],
                details="Employee restored to the active roster.",
            )
            rebalance_scheduled_future()
            return jsonify({"id": employee["id"], "name": employee["name"]}), 200
        write_activity(
            get_db(),
            "Employee added",
            employee_name=name.strip(),
            details="Employee added to the active roster.",
        )
        rebalance_scheduled_future()
        return jsonify({"id": cursor.lastrowid, "name": name.strip()}), 201

    @app.patch("/api/employees/<int:employee_id>")
    def rename_employee(employee_id: int):
        payload = request.get_json(silent=True) or {}
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            return api_error("Enter an employee name.", 400)
        name = name.strip()
        if len(name) > 80:
            return api_error("Employee names must be 80 characters or fewer.", 400)
        db = get_db()
        employee = db.execute(
            "SELECT id, name FROM employees WHERE id = ? AND active = 1",
            (employee_id,),
        ).fetchone()
        if employee is None:
            return api_error("That active employee was not found.", 404)
        try:
            db.execute(
                "UPDATE employees SET name = ? WHERE id = ?", (name, employee_id)
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            return api_error("An employee with that name already exists.", 409)
        if employee["name"] != name:
            write_activity(
                db,
                "Employee renamed",
                employee_name=name,
                details=f"Changed name from {employee['name']} to {name}.",
            )
        return jsonify({"id": employee_id, "name": name})

    @app.delete("/api/employees/<int:employee_id>")
    def remove_employee(employee_id: int):
        db = get_db()
        employee = db.execute(
            "SELECT id, name FROM employees WHERE id = ? AND active = 1",
            (employee_id,),
        ).fetchone()
        if employee is None:
            return api_error("That active employee was not found.", 404)
        active_count = db.execute(
            "SELECT COUNT(*) FROM employees WHERE active = 1"
        ).fetchone()[0]
        if active_count <= MIN_ACTIVE_EMPLOYEES:
            return api_error(
                "At least three active employees must remain in the roster.",
                409,
            )
        db.execute("UPDATE employees SET active = 0 WHERE id = ?", (employee_id,))
        db.commit()
        write_activity(
            db,
            "Employee removed",
            employee_name=employee["name"],
            details="Employee deactivated; assignment history retained.",
        )
        rebalance_scheduled_future()
        return jsonify({"ok": True, "removed": employee["name"]})

    @app.route("/api/employees/<int:employee_id>/availability", methods=["GET", "PUT"])
    def employee_availability(employee_id: int):
        db = get_db()
        employee = db.execute(
            "SELECT id, name FROM employees WHERE id = ? AND active = 1",
            (employee_id,),
        ).fetchone()
        if employee is None:
            return api_error("That active employee was not found.", 404)
        if request.method == "GET":
            rows = db.execute(
                "SELECT weekday FROM employee_unavailable_weekdays WHERE employee_id = ?",
                (employee_id,),
            ).fetchall()
            return jsonify({"weekdays": [row["weekday"] for row in rows]})

        payload = request.get_json(silent=True) or {}
        weekdays = payload.get("weekdays")
        if (
            not isinstance(weekdays, list)
            or any(
                not isinstance(day, int)
                or isinstance(day, bool)
                or day < 0
                or day > 6
                for day in weekdays
            )
            or len(set(weekdays)) != len(weekdays)
        ):
            return api_error("Choose unique weekdays from Monday (0) to Sunday (6).", 400)
        db.execute(
            "DELETE FROM employee_unavailable_weekdays WHERE employee_id = ?",
            (employee_id,),
        )
        db.executemany(
            "INSERT INTO employee_unavailable_weekdays (employee_id, weekday) VALUES (?, ?)",
            [(employee_id, weekday) for weekday in weekdays],
        )
        db.commit()
        weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        write_activity(
            db,
            "Recurring availability updated",
            employee_name=employee["name"],
            details=(
                "Unavailable: "
                + (", ".join(weekday_names[weekday] for weekday in weekdays) or "none")
            ),
        )
        rebalance_scheduled_future()
        return jsonify({"weekdays": weekdays})

    @app.get("/api/pair-exclusions")
    def list_pair_exclusions():
        rows = get_db().execute(
            """
            SELECT x.employee1_id, e1.name AS employee1_name,
                   x.employee2_id, e2.name AS employee2_name, x.created_at
            FROM employee_pair_exclusions x
            JOIN employees e1 ON e1.id = x.employee1_id
            JOIN employees e2 ON e2.id = x.employee2_id
            ORDER BY e1.name COLLATE NOCASE, e2.name COLLATE NOCASE
            """
        ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.post("/api/pair-exclusions")
    def add_pair_exclusion():
        payload = request.get_json(silent=True) or {}
        employee_ids = payload.get("employee_ids")
        if (
            not isinstance(employee_ids, list)
            or len(employee_ids) != 2
            or any(
                not isinstance(employee_id, int) or isinstance(employee_id, bool)
                for employee_id in employee_ids
            )
            or employee_ids[0] == employee_ids[1]
        ):
            return api_error("Choose exactly two different employees.", 400)
        employee1_id, employee2_id = sorted(employee_ids)
        db = get_db()
        employees = db.execute(
            """
            SELECT id, name FROM employees
            WHERE active = 1 AND id IN (?, ?)
            ORDER BY id
            """,
            (employee1_id, employee2_id),
        ).fetchall()
        if len(employees) != 2:
            return api_error("Both employees must be active.", 400)
        conflicting_manual = db.execute(
            """
            SELECT date FROM assignments
            WHERE date >= ? AND manual = 1
              AND (
                (employee1_id = ? AND employee2_id = ?)
                OR (employee1_id = ? AND employee2_id = ?)
              )
            ORDER BY date
            LIMIT 1
            """,
            (
                date.today().isoformat(),
                employee1_id,
                employee2_id,
                employee2_id,
                employee1_id,
            ),
        ).fetchone()
        if conflicting_manual is not None:
            return api_error(
                "This pair has a future manual assignment. Change that assignment "
                "before adding the restriction.",
                409,
            )
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO employee_pair_exclusions
                (employee1_id, employee2_id)
            VALUES (?, ?)
            """,
            (employee1_id, employee2_id),
        )
        db.commit()
        if cursor.rowcount:
            names = [employee["name"] for employee in employees]
            write_activity(
                db,
                "Employee pairing restricted",
                details=f"{names[0]} and {names[1]} cannot be assigned together.",
            )
            rebalance_scheduled_future()
        return jsonify({"ok": True, "created": bool(cursor.rowcount)}), (
            201 if cursor.rowcount else 200
        )

    @app.delete("/api/pair-exclusions/<int:employee1_id>/<int:employee2_id>")
    def remove_pair_exclusion(employee1_id: int, employee2_id: int):
        if employee1_id == employee2_id:
            return api_error("Choose two different employees.", 400)
        first_id, second_id = sorted((employee1_id, employee2_id))
        db = get_db()
        names = db.execute(
            """
            SELECT id, name FROM employees
            WHERE id IN (?, ?)
            ORDER BY id
            """,
            (first_id, second_id),
        ).fetchall()
        cursor = db.execute(
            """
            DELETE FROM employee_pair_exclusions
            WHERE employee1_id = ? AND employee2_id = ?
            """,
            (first_id, second_id),
        )
        db.commit()
        if not cursor.rowcount:
            return api_error("That pairing restriction was not found.", 404)
        write_activity(
            db,
            "Employee pairing restriction removed",
            details=(
                f"Removed restriction for {' and '.join(row['name'] for row in names)}."
            ),
        )
        rebalance_scheduled_future()
        return jsonify({"ok": True})

    @app.get("/api/holidays")
    def holiday_for_date():
        try:
            day = date.fromisoformat(request.args.get("date", ""))
        except ValueError:
            return api_error("Provide a valid date.", 400)
        holiday = get_db().execute(
            "SELECT date, name, dc_closed FROM holidays WHERE date = ?",
            (day.isoformat(),),
        ).fetchone()
        if holiday is None:
            return jsonify({"holiday": None})
        return jsonify(
            {
                "holiday": {
                    "date": holiday["date"],
                    "name": holiday["name"],
                    "dc_closed": bool(holiday["dc_closed"]),
                }
            }
        )

    @app.put("/api/holidays/<day_text>")
    def save_holiday(day_text: str):
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            return api_error("Provide a valid date.", 400)
        payload = request.get_json(silent=True) or {}
        name = payload.get("name", "")
        dc_closed = payload.get("dc_closed")
        if not isinstance(name, str) or len(name.strip()) > 80:
            return api_error("Holiday names must be 80 characters or fewer.", 400)
        if not isinstance(dc_closed, bool):
            return api_error("Choose whether the DC is closed for this holiday.", 400)
        db = get_db()
        existing_holiday = db.execute(
            "SELECT name, dc_closed FROM holidays WHERE date = ?",
            (day.isoformat(),),
        ).fetchone()
        db.execute(
            """
            INSERT INTO holidays (date, name, dc_closed) VALUES (?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                name = excluded.name,
                dc_closed = excluded.dc_closed
            """,
            (day.isoformat(), name.strip(), int(dc_closed)),
        )
        db.commit()
        write_activity(
            db,
            "Holiday updated" if existing_holiday else "Holiday added",
            date=day.isoformat(),
            details=(
                f"{name.strip() or 'Holiday'}; "
                f"DC {'closed' if dc_closed else 'open'}."
            ),
        )
        if day.weekday() == 5:
            if dc_closed and day >= date.today():
                db.execute("DELETE FROM assignments WHERE date = ?", (day.isoformat(),))
                db.commit()
            elif not dc_closed:
                ensure_assignments(day, day + timedelta(days=1))
        return jsonify({"ok": True})

    @app.delete("/api/holidays/<day_text>")
    def delete_holiday(day_text: str):
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            return api_error("Provide a valid date.", 400)
        db = get_db()
        holiday = db.execute(
            "SELECT name FROM holidays WHERE date = ?", (day.isoformat(),)
        ).fetchone()
        cursor = db.execute("DELETE FROM holidays WHERE date = ?", (day.isoformat(),))
        db.commit()
        if cursor.rowcount:
            write_activity(
                db,
                "Holiday removed",
                date=day.isoformat(),
                details=holiday["name"] or "Holiday",
            )
        if day.weekday() == 5:
            ensure_assignments(day, day + timedelta(days=1))
        return jsonify({"ok": True})

    @app.get("/api/calendar")
    def calendar_events():
        period = requested_period()
        if not isinstance(period[0], date):
            return period
        start, end = period
        active_count = get_db().execute(
            "SELECT COUNT(*) FROM employees WHERE active = 1"
        ).fetchone()[0]
        rows = []
        if active_count >= MIN_ACTIVE_EMPLOYEES:
            ensure_assignments(start, end)
            rows = get_db().execute(
                """
                SELECT a.date, a.manual,
                       e1.id AS employee1_id, e1.name AS employee1_name,
                       e2.id AS employee2_id, e2.name AS employee2_name
                FROM assignments a
                JOIN employees e1 ON e1.id = a.employee1_id
                JOIN employees e2 ON e2.id = a.employee2_id
                WHERE a.date >= ? AND a.date < ?
                ORDER BY a.date
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        leave_rows = get_db().execute(
            """
            SELECT l.date, e.id, e.name
            FROM leave_days l
            JOIN employees e ON e.id = l.employee_id
            WHERE l.date >= ? AND l.date < ?
            ORDER BY e.name COLLATE NOCASE
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        leave_by_date: dict[str, list[dict[str, Any]]] = {}
        for leave in leave_rows:
            leave_by_date.setdefault(leave["date"], []).append(
                {"id": leave["id"], "name": leave["name"]}
            )
        events = [
                {
                    "id": row["date"],
                    "title": f'{row["employee1_name"]} + {row["employee2_name"]}',
                    "start": row["date"],
                    "allDay": True,
                    "backgroundColor": "#e8f1ff",
                    "borderColor": "#bfd4f7",
                    "textColor": "#173b72",
                    "extendedProps": {
                        "employee_ids": [row["employee1_id"], row["employee2_id"]],
                        "employee_names": [
                            row["employee1_name"],
                            row["employee2_name"],
                        ],
                        "leave_employee_ids": [
                            employee["id"]
                            for employee in leave_by_date.get(row["date"], [])
                        ],
                        "leave_names": [
                            employee["name"]
                            for employee in leave_by_date.get(row["date"], [])
                        ],
                        "manual": bool(row["manual"]),
                    },
                }
                for row in rows
            ]
        for gap in coverage_gaps(start, end):
            events.append(
                {
                    "id": f'gap-{gap["date"]}',
                    "title": f'Coverage needed ({gap["available"]} available)',
                    "start": gap["date"],
                    "allDay": True,
                    "backgroundColor": "#fff0f0",
                    "borderColor": "#f3c4c4",
                    "textColor": "#a72d38",
                    "extendedProps": {
                        "coverage_issue": True,
                        "leave_names": [
                            employee["name"]
                            for employee in leave_by_date.get(gap["date"], [])
                        ],
                    },
                }
            )
        holidays = get_db().execute(
            """
            SELECT date, name, dc_closed FROM holidays
            WHERE date >= ? AND date < ? ORDER BY date
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        for holiday in holidays:
            label = holiday["name"].strip() or "Holiday"
            status = "DC closed" if holiday["dc_closed"] else "DC open"
            events.append(
                {
                    "id": f'holiday-{holiday["date"]}',
                    "title": f"{label} · {status}",
                    "start": holiday["date"],
                    "allDay": True,
                    "backgroundColor": "#fff4d6",
                    "borderColor": "#f1d995",
                    "textColor": "#72551b",
                    "classNames": ["holiday-event"],
                    "extendedProps": {
                        "holiday": True,
                        "dc_closed": bool(holiday["dc_closed"]),
                        "leave_names": (
                            [
                                employee["name"]
                                for employee in leave_by_date.get(holiday["date"], [])
                            ]
                            if holiday["dc_closed"]
                            else []
                        ),
                    },
                }
            )
        attendance_dates = {
            row["date"]
            for row in get_db().execute(
                """
                SELECT date FROM attendance_records
                WHERE date >= ? AND date < ?
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        }
        attendance_rows = get_db().execute(
            """
            SELECT a.date, e.id, e.name
            FROM attendance_employees a
            JOIN employees e ON e.id = a.employee_id
            WHERE a.date >= ? AND a.date < ?
            ORDER BY e.name COLLATE NOCASE
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        attendance_by_date: dict[str, list[dict[str, Any]]] = {}
        for attendance in attendance_rows:
            attendance_by_date.setdefault(attendance["date"], []).append(
                {"id": attendance["id"], "name": attendance["name"]}
            )
        existing_event_dates: set[str] = set()
        for event in events:
            event_date = event["start"]
            if event_date in attendance_dates and event_date not in existing_event_dates:
                actual = attendance_by_date.get(event_date, [])
                event["extendedProps"]["attendance_recorded"] = True
                event["extendedProps"]["actual_employee_ids"] = [
                    employee["id"] for employee in actual
                ]
                event["extendedProps"]["actual_names"] = [
                    employee["name"] for employee in actual
                ]
                existing_event_dates.add(event_date)
        for attendance_date in sorted(attendance_dates - existing_event_dates):
            actual = attendance_by_date.get(attendance_date, [])
            events.append(
                {
                    "id": f"attendance-{attendance_date}",
                    "title": "Attendance recorded",
                    "start": attendance_date,
                    "allDay": True,
                    "backgroundColor": "#e8f6ef",
                    "borderColor": "#bde4cd",
                    "textColor": "#236242",
                    "extendedProps": {
                        "attendance_recorded": True,
                        "actual_employee_ids": [
                            employee["id"] for employee in actual
                        ],
                        "actual_names": [
                            employee["name"] for employee in actual
                        ],
                    },
                }
            )
        return jsonify(events)

    @app.get("/api/simulate/leave")
    def simulate_leave():
        try:
            day = date.fromisoformat(request.args.get("date", ""))
        except ValueError:
            return api_error("Provide a valid Saturday date.", 400)
        if day.isoformat() != request.args.get("date") or day.weekday() != 5:
            return api_error("Choose a valid Saturday.", 400)
        if day < date.today():
            return api_error("What-if previews are available for today or future dates.", 400)
        try:
            employee_id = int(request.args.get("employee_id", ""))
        except ValueError:
            return api_error("Choose an active employee.", 400)
        source_db = get_db()
        employee = source_db.execute(
            "SELECT id, name FROM employees WHERE id = ? AND active = 1",
            (employee_id,),
        ).fetchone()
        if employee is None:
            return api_error("Choose an active employee.", 400)

        simulation_db = sqlite3.connect(":memory:")
        simulation_db.row_factory = sqlite3.Row
        simulation_db.execute("PRAGMA foreign_keys = ON")
        original_db = g.db
        try:
            source_db.backup(simulation_db)
            g.db = simulation_db
            month_start = day.replace(day=1)
            month_end = (month_start + timedelta(days=32)).replace(day=1)
            ensure_assignments(month_start, month_end)

            def month_assignments() -> list[sqlite3.Row]:
                return get_db().execute(
                    """
                    SELECT a.date, a.employee1_id, a.employee2_id, a.manual,
                           e1.name AS employee1_name, e2.name AS employee2_name
                    FROM assignments a
                    JOIN employees e1 ON e1.id = a.employee1_id
                    JOIN employees e2 ON e2.id = a.employee2_id
                    WHERE a.date >= ? AND a.date < ?
                    ORDER BY a.date
                    """,
                    (month_start.isoformat(), month_end.isoformat()),
                ).fetchall()

            baseline_rows = month_assignments()
            baseline_target = next(
                (row for row in baseline_rows if row["date"] == day.isoformat()),
                None,
            )
            baseline_counts = {
                row["id"]: 0
                for row in get_db().execute(
                    "SELECT id FROM employees WHERE active = 1"
                ).fetchall()
            }
            for row in baseline_rows:
                for assigned_id in (row["employee1_id"], row["employee2_id"]):
                    if assigned_id in baseline_counts:
                        baseline_counts[assigned_id] += 1

            get_db().execute(
                "INSERT OR IGNORE INTO leave_days (employee_id, date) VALUES (?, ?)",
                (employee_id, day.isoformat()),
            )
            ensure_assignments(month_start, month_end)
            simulated_rows = month_assignments()
            simulated_target = next(
                (row for row in simulated_rows if row["date"] == day.isoformat()),
                None,
            )
            simulated_counts = {employee_key: 0 for employee_key in baseline_counts}
            for row in simulated_rows:
                for assigned_id in (row["employee1_id"], row["employee2_id"]):
                    if assigned_id in simulated_counts:
                        simulated_counts[assigned_id] += 1

            holiday = get_db().execute(
                "SELECT dc_closed FROM holidays WHERE date = ?",
                (day.isoformat(),),
            ).fetchone()
            dc_closed = holiday is not None and bool(holiday["dc_closed"])
            available_count = get_db().execute(
                """
                SELECT COUNT(*) FROM employees e
                WHERE e.active = 1
                  AND NOT EXISTS (
                      SELECT 1 FROM leave_days l
                      WHERE l.employee_id = e.id AND l.date = ?
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM employee_unavailable_weekdays w
                      WHERE w.employee_id = e.id AND w.weekday = ?
                  )
                """,
                (day.isoformat(), day.weekday()),
            ).fetchone()[0]
            employee_rows = get_db().execute(
                "SELECT id, name FROM employees WHERE active = 1 ORDER BY name COLLATE NOCASE"
            ).fetchall()
            workload = [
                {
                    "employee_id": row["id"],
                    "name": row["name"],
                    "scheduled_before": baseline_counts[row["id"]],
                    "scheduled_after": simulated_counts[row["id"]],
                }
                for row in employee_rows
            ]

            def assigned_names(row: sqlite3.Row | None) -> list[str]:
                if row is None:
                    return []
                return [row["employee1_name"], row["employee2_name"]]

            proposed_names = assigned_names(simulated_target)
            if dc_closed:
                message = "The DC is closed; no Saturday team is required."
            elif len(employee_rows) < MIN_ACTIVE_EMPLOYEES:
                message = f"At least {MIN_ACTIVE_EMPLOYEES} active employees are required."
            elif len(proposed_names) == 2:
                message = "Two employees are available and assigned."
            else:
                message = (
                    f"Coverage gap: only {available_count} employee(s) are available; "
                    "two are required."
                )
            return jsonify(
                {
                    "date": day.isoformat(),
                    "employee_name": employee["name"],
                    "dc_closed": dc_closed,
                    "coverage_met": dc_closed or len(proposed_names) == 2,
                    "message": message,
                    "scheduled_before": assigned_names(baseline_target),
                    "scheduled_after": proposed_names,
                    "workload": workload,
                }
            )
        finally:
            g.db = original_db
            simulation_db.close()

    @app.get("/api/simulate/roster")
    def simulate_roster_change():
        try:
            day = date.fromisoformat(request.args.get("date", ""))
        except ValueError:
            return api_error("Provide a valid Saturday date.", 400)
        if day.isoformat() != request.args.get("date") or day.weekday() != 5:
            return api_error("Choose a valid Saturday.", 400)
        if day < date.today():
            return api_error("Roster previews are available for today or future dates.", 400)
        action = request.args.get("action", "")
        if action not in {"add", "remove"}:
            return api_error("Choose whether to add or remove an employee.", 400)

        source_db = get_db()
        if action == "add":
            name = request.args.get("name", "").strip()
            if not name or len(name) > 80:
                return api_error("Enter an employee name up to 80 characters.", 400)
            existing = source_db.execute(
                "SELECT id, name, active FROM employees WHERE name = ?",
                (name,),
            ).fetchone()
            if existing is not None and existing["active"]:
                return api_error("That employee is already active.", 409)
        else:
            try:
                employee_id = int(request.args.get("employee_id", ""))
            except ValueError:
                return api_error("Choose an active employee.", 400)
            existing = source_db.execute(
                "SELECT id, name, active FROM employees WHERE id = ? AND active = 1",
                (employee_id,),
            ).fetchone()
            if existing is None:
                return api_error("Choose an active employee.", 400)
            name = existing["name"]

        simulation_db = sqlite3.connect(":memory:")
        simulation_db.row_factory = sqlite3.Row
        simulation_db.execute("PRAGMA foreign_keys = ON")
        original_db = g.db
        try:
            source_db.backup(simulation_db)
            g.db = simulation_db
            horizon_start = day
            horizon_end = day + timedelta(days=90)
            ensure_assignments(horizon_start, horizon_end)

            def horizon_assignments() -> list[sqlite3.Row]:
                if (
                    get_db().execute(
                        "SELECT COUNT(*) FROM employees WHERE active = 1"
                    ).fetchone()[0]
                    < MIN_ACTIVE_EMPLOYEES
                ):
                    return []
                return get_db().execute(
                    """
                    SELECT a.date, a.employee1_id, a.employee2_id,
                           e1.name AS employee1_name, e2.name AS employee2_name
                    FROM assignments a
                    JOIN employees e1 ON e1.id = a.employee1_id AND e1.active = 1
                    JOIN employees e2 ON e2.id = a.employee2_id AND e2.active = 1
                    WHERE a.date >= ? AND a.date < ?
                    ORDER BY a.date
                    """,
                    (horizon_start.isoformat(), horizon_end.isoformat()),
                ).fetchall()

            def assignment_counts(rows: list[sqlite3.Row]) -> dict[int, int]:
                counts = {
                    row["id"]: 0
                    for row in get_db().execute(
                        "SELECT id FROM employees WHERE active = 1"
                    ).fetchall()
                }
                for row in rows:
                    for assigned_id in (row["employee1_id"], row["employee2_id"]):
                        if assigned_id in counts:
                            counts[assigned_id] += 1
                return counts

            before_rows = horizon_assignments()
            before_counts = assignment_counts(before_rows)
            simulated_db = get_db()
            if action == "add":
                if existing is None:
                    simulated_cursor = simulated_db.execute(
                        "INSERT INTO employees (name) VALUES (?)", (name,)
                    )
                    simulated_employee_id = simulated_cursor.lastrowid
                else:
                    simulated_db.execute(
                        "UPDATE employees SET active = 1 WHERE id = ?",
                        (existing["id"],),
                    )
                    simulated_employee_id = existing["id"]
            else:
                simulated_employee_id = existing["id"]
                simulated_db.execute(
                    "UPDATE employees SET active = 0 WHERE id = ?",
                    (simulated_employee_id,),
                )
            simulated_db.commit()
            rebalance_scheduled_future(day)
            ensure_assignments(horizon_start, horizon_end)
            after_rows = horizon_assignments()
            after_counts = assignment_counts(after_rows)
            all_employees = simulated_db.execute(
                "SELECT id, name FROM employees ORDER BY name COLLATE NOCASE"
            ).fetchall()
            before_active_ids = {
                row["id"]
                for row in source_db.execute(
                    "SELECT id FROM employees WHERE active = 1"
                ).fetchall()
            }
            workload = [
                {
                    "name": row["name"],
                    "active_after": row["id"] in after_counts,
                    "scheduled_before": before_counts.get(row["id"], 0),
                    "scheduled_after": after_counts.get(row["id"], 0),
                }
                for row in all_employees
                if row["id"] in before_active_ids or row["id"] == simulated_employee_id
            ]

            def assignment_payload(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
                return [
                    {
                        "date": row["date"],
                        "employees": [row["employee1_name"], row["employee2_name"]],
                    }
                    for row in rows
                    if date.fromisoformat(row["date"]) >= day
                ]

            coverage_gaps = []
            saturday = horizon_start + timedelta(
                days=(5 - horizon_start.weekday()) % 7
            )
            while saturday < horizon_end:
                if saturday >= day:
                    holiday = simulated_db.execute(
                        "SELECT dc_closed FROM holidays WHERE date = ?",
                        (saturday.isoformat(),),
                    ).fetchone()
                    if holiday is None or not holiday["dc_closed"]:
                        available_count = simulated_db.execute(
                            """
                            SELECT COUNT(*) FROM employees e
                            WHERE e.active = 1
                              AND NOT EXISTS (
                                SELECT 1 FROM leave_days l
                                WHERE l.employee_id = e.id AND l.date = ?
                              )
                              AND NOT EXISTS (
                                SELECT 1 FROM employee_unavailable_weekdays w
                                WHERE w.employee_id = e.id AND w.weekday = ?
                              )
                            """,
                            (saturday.isoformat(), saturday.weekday()),
                        ).fetchone()[0]
                        if (
                            available_count < 2
                            or len(after_counts) < MIN_ACTIVE_EMPLOYEES
                        ):
                            coverage_gaps.append(
                                {
                                    "date": saturday.isoformat(),
                                    "available": available_count,
                                }
                            )
                saturday += timedelta(days=7)

            return jsonify(
                {
                    "action": action,
                    "employee_name": name,
                    "date": day.isoformat(),
                    "horizon_start": horizon_start.isoformat(),
                    "horizon_end": horizon_end.isoformat(),
                    "active_employee_count": len(after_counts),
                    "coverage_gaps": coverage_gaps,
                    "assignments_before": assignment_payload(before_rows),
                    "assignments_after": assignment_payload(after_rows),
                    "workload": workload,
                }
            )
        finally:
            g.db = original_db
            simulation_db.close()

    @app.get("/api/summary")
    def workload_summary():
        period = requested_period()
        if not isinstance(period[0], date):
            return period
        start, end = period
        active_count = get_db().execute(
            "SELECT COUNT(*) FROM employees WHERE active = 1"
        ).fetchone()[0]
        if active_count >= MIN_ACTIVE_EMPLOYEES:
            ensure_assignments(start, end)
        if active_count >= MIN_ACTIVE_EMPLOYEES:
            rows = get_db().execute(
                """
                SELECT e.id, e.name, COUNT(a.date) AS saturday_count,
                       (
                           SELECT COUNT(*)
                           FROM attendance_employees ae
                           JOIN attendance_records ar ON ar.date = ae.date
                           WHERE ae.employee_id = e.id
                             AND ae.date >= ? AND ae.date < ?
                       ) AS worked_count
                FROM employees e
                LEFT JOIN assignments a
                  ON (a.employee1_id = e.id OR a.employee2_id = e.id)
                 AND a.date >= ? AND a.date < ?
                WHERE e.active = 1
                GROUP BY e.id
                ORDER BY e.name COLLATE NOCASE
                """,
                (
                    start.isoformat(),
                    end.isoformat(),
                    start.isoformat(),
                    end.isoformat(),
                ),
            ).fetchall()
        else:
            rows = get_db().execute(
                """
                SELECT e.id, e.name, 0 AS saturday_count,
                       (
                           SELECT COUNT(*)
                           FROM attendance_employees ae
                           WHERE ae.employee_id = e.id
                             AND ae.date >= ? AND ae.date < ?
                       ) AS worked_count
                FROM employees e WHERE active = 1
                ORDER BY name COLLATE NOCASE
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        counts = [row["saturday_count"] for row in rows]
        saturday_total = sum(
            1
            for day_offset in range((end - start).days)
            if (start + timedelta(days=day_offset)).weekday() == 5
        )
        employee_payload = [
            {
                **dict(row),
                "saturdays_off_count": max(0, saturday_total - row["saturday_count"]),
            }
            for row in rows
        ]
        spread = max(counts, default=0) - min(counts, default=0) if counts else 0
        worked_counts = [row["worked_count"] for row in rows]
        actual_work_spread = (
            max(worked_counts, default=0) - min(worked_counts, default=0)
            if worked_counts
            else 0
        )
        gaps = coverage_gaps(start, end)
        monthly_shortfalls: list[dict[str, Any]] = []
        months: set[tuple[int, int]] = set()
        first_month = start.replace(day=1)
        month_cursor = first_month
        while month_cursor < end:
            months.add((month_cursor.year, month_cursor.month))
            if month_cursor.month == 12:
                month_cursor = month_cursor.replace(
                    year=month_cursor.year + 1, month=1
                )
            else:
                month_cursor = month_cursor.replace(month=month_cursor.month + 1)
        for year, month_number in sorted(months):
            month_start = date(year, month_number, 1)
            month_end = (
                date(year + 1, 1, 1)
                if month_number == 12
                else date(year, month_number + 1, 1)
            )
            first_saturday = month_start + timedelta(
                days=(5 - month_start.weekday()) % 7
            )
            saturdays_in_month = (
                0
                if first_saturday >= month_end
                else 1 + (month_end - timedelta(days=1) - first_saturday).days // 7
            )
            assignment_limit = max(
                0, saturdays_in_month - MIN_SATURDAYS_OFF
            )
            monthly_assignments = get_db().execute(
                """
                SELECT employee1_id, employee2_id
                FROM assignments
                WHERE date >= ? AND date < ?
                """,
                (month_start.isoformat(), month_end.isoformat()),
            ).fetchall()
            monthly_counts = {row["id"]: 0 for row in rows}
            for assignment in monthly_assignments:
                for employee_id in (
                    assignment["employee1_id"],
                    assignment["employee2_id"],
                ):
                    if employee_id in monthly_counts:
                        monthly_counts[employee_id] += 1
            for employee in rows:
                if monthly_counts[employee["id"]] > assignment_limit:
                    monthly_shortfalls.append(
                        {
                            "month": month_start.strftime("%B %Y"),
                            "employee": employee["name"],
                            "saturdays_off": saturdays_in_month
                            - monthly_counts[employee["id"]],
                        }
                    )
        warning = None
        if active_count < MIN_ACTIVE_EMPLOYEES:
            warning = (
                f"At least {MIN_ACTIVE_EMPLOYEES} active employees are required; "
                f"add {MIN_ACTIVE_EMPLOYEES - active_count} more to schedule Saturdays."
            )
        elif gaps:
            warning = f"{len(gaps)} Saturday(s) have fewer than two employees available."
        elif monthly_shortfalls:
            months_with_shortfalls = sorted(
                {item["month"] for item in monthly_shortfalls}
            )
            warning = (
                "The minimum of "
                f"{MIN_SATURDAYS_OFF} Saturdays off per employee cannot be met "
                f"for everyone in {', '.join(months_with_shortfalls)} while "
                "keeping two employees assigned each open Saturday. Add staff "
                "or review leave, availability, manual assignments, and pairing restrictions."
            )
        elif spread > 1:
            warning = "Workload differs by more than one Saturday this period."
        actual_work_warning = (
            "Actual Saturdays worked differ by more than one employee this period."
            if actual_work_spread > 1
            else None
        )
        return jsonify(
            {
                "employees": employee_payload,
                "spread": spread,
                "actual_work_spread": actual_work_spread,
                "warning": warning,
                "actual_work_warning": actual_work_warning,
                "coverage_gaps": len(gaps),
                "active_employee_count": active_count,
                "minimum_employee_count": MIN_ACTIVE_EMPLOYEES,
                "minimum_saturdays_off": MIN_SATURDAYS_OFF,
                "saturday_off_shortfalls": monthly_shortfalls,
            }
        )

    @app.get("/api/leave")
    def leave_for_date():
        try:
            day = date.fromisoformat(request.args.get("date", ""))
        except ValueError:
            return api_error("Provide a valid date.", 400)
        rows = get_db().execute(
            "SELECT employee_id FROM leave_days WHERE date = ?", (day.isoformat(),)
        ).fetchall()
        return jsonify({"employee_ids": [row["employee_id"] for row in rows]})

    @app.get("/api/leave-requests")
    def list_leave_requests():
        status_filter = request.args.get("status")
        if status_filter not in (None, "pending", "approved", "rejected"):
            return api_error("Choose a valid leave request status.", 400)
        query = """
            SELECT r.id, r.employee_id, e.name AS employee_name,
                   r.start_date, r.end_date, r.status, r.created_at
            FROM leave_requests r
            JOIN employees e ON e.id = r.employee_id
        """
        params: tuple[str, ...] = ()
        if status_filter:
            query += " WHERE r.status = ?"
            params = (status_filter,)
        query += """
            ORDER BY CASE r.status WHEN 'pending' THEN 0 ELSE 1 END,
                     r.start_date, r.id
        """
        db = get_db()
        rows = db.execute(query, params).fetchall()
        requests = [dict(row) for row in rows]
        active_count = db.execute(
            "SELECT COUNT(*) FROM employees WHERE active = 1"
        ).fetchone()[0]
        for leave_request in requests:
            if leave_request["status"] != "pending":
                continue
            request_start = max(
                date.fromisoformat(leave_request["start_date"]),
                date.today(),
            )
            request_end = date.fromisoformat(leave_request["end_date"])
            saturday = request_start + timedelta(
                days=(5 - request_start.weekday()) % 7
            )
            considered = 0
            risks: list[dict[str, Any]] = []
            while saturday <= request_end:
                day_text = saturday.isoformat()
                holiday = db.execute(
                    "SELECT dc_closed FROM holidays WHERE date = ?",
                    (day_text,),
                ).fetchone()
                if holiday is None or not holiday["dc_closed"]:
                    considered += 1
                    available = db.execute(
                        """
                        SELECT COUNT(*) FROM employees e
                        WHERE e.active = 1
                          AND NOT EXISTS (
                              SELECT 1 FROM leave_days l
                              WHERE l.employee_id = e.id AND l.date = ?
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM employee_unavailable_weekdays w
                              WHERE w.employee_id = e.id AND w.weekday = ?
                          )
                        """,
                        (day_text, saturday.weekday()),
                    ).fetchone()[0]
                    request_employee_available = db.execute(
                        """
                        SELECT 1 FROM employees e
                        WHERE e.id = ? AND e.active = 1
                          AND NOT EXISTS (
                              SELECT 1 FROM leave_days l
                              WHERE l.employee_id = e.id AND l.date = ?
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM employee_unavailable_weekdays w
                              WHERE w.employee_id = e.id AND w.weekday = ?
                          )
                        """,
                        (
                            leave_request["employee_id"],
                            day_text,
                            saturday.weekday(),
                        ),
                    ).fetchone()
                    available_after = available - int(
                        request_employee_available is not None
                    )
                    if available_after < 2 or active_count < MIN_ACTIVE_EMPLOYEES:
                        risks.append(
                            {
                                "date": day_text,
                                "available_after_approval": available_after,
                            }
                        )
                saturday += timedelta(days=7)
            leave_request["coverage_forecast"] = {
                "open_saturdays_considered": considered,
                "coverage_risks": risks,
            }
        return jsonify(requests)

    @app.post("/api/leave-requests")
    def create_leave_request():
        payload = request.get_json(silent=True) or {}
        employee_id = payload.get("employee_id")
        if not isinstance(employee_id, int) or isinstance(employee_id, bool):
            return api_error("Choose an active employee.", 400)
        try:
            start = date.fromisoformat(payload.get("start_date", ""))
            end = date.fromisoformat(payload.get("end_date", ""))
        except (TypeError, ValueError):
            return api_error("Provide valid leave start and end dates.", 400)
        if end < start:
            return api_error("Leave end date must be on or after the start date.", 400)
        if (end - start).days > 370:
            return api_error("A leave request cannot exceed 371 calendar days.", 400)

        db = get_db()
        employee = db.execute(
            "SELECT id, name FROM employees WHERE id = ? AND active = 1",
            (employee_id,),
        ).fetchone()
        if employee is None:
            return api_error("Choose an active employee.", 400)
        conflict = db.execute(
            """
            SELECT 1 FROM leave_requests
            WHERE employee_id = ? AND status = 'pending'
              AND start_date <= ? AND end_date >= ?
            UNION ALL
            SELECT 1 FROM leave_days
            WHERE employee_id = ? AND date BETWEEN ? AND ?
            LIMIT 1
            """,
            (employee_id, end.isoformat(), start.isoformat(),
             employee_id, start.isoformat(), end.isoformat()),
        ).fetchone()
        if conflict is not None:
            return api_error(
                "This request overlaps existing approved leave or a pending request.",
                409,
            )
        cursor = db.execute(
            """
            INSERT INTO leave_requests (employee_id, start_date, end_date)
            VALUES (?, ?, ?)
            """,
            (employee_id, start.isoformat(), end.isoformat()),
        )
        db.commit()
        write_activity(
            db,
            "Leave request submitted",
            employee_name=employee["name"],
            date=start.isoformat(),
            details=f"Requested leave through {end.isoformat()}; pending approval.",
        )
        return jsonify({"id": cursor.lastrowid, "status": "pending"}), 201

    @app.patch("/api/leave-requests/<int:request_id>")
    def update_leave_request(request_id: int):
        payload = request.get_json(silent=True) or {}
        status = payload.get("status")
        if status not in {"approved", "rejected"}:
            return api_error("Choose approved or rejected.", 400)
        db = get_db()
        leave_request = db.execute(
            """
            SELECT id, employee_id, start_date, end_date, status
            FROM leave_requests WHERE id = ?
            """,
            (request_id,),
        ).fetchone()
        if leave_request is None:
            return api_error("That leave request was not found.", 404)
        if leave_request["status"] != "pending":
            return api_error("Only pending leave requests can be reviewed.", 409)
        if status == "approved":
            employee = db.execute(
                "SELECT active FROM employees WHERE id = ?",
                (leave_request["employee_id"],),
            ).fetchone()
            if employee is None or not employee["active"]:
                return api_error(
                    "Reactivate this employee before approving their leave.",
                    409,
                )
            existing_leave = db.execute(
                """
                SELECT 1 FROM leave_days
                WHERE employee_id = ? AND date BETWEEN ? AND ?
                LIMIT 1
                """,
                (
                    leave_request["employee_id"],
                    leave_request["start_date"],
                    leave_request["end_date"],
                ),
            ).fetchone()
            if existing_leave is not None:
                return api_error(
                    "This request overlaps existing approved leave.",
                    409,
                )
            start = date.fromisoformat(leave_request["start_date"])
            end = date.fromisoformat(leave_request["end_date"])
            days = [
                (leave_request["employee_id"], (start + timedelta(days=offset)).isoformat())
                for offset in range((end - start).days + 1)
            ]
            db.executemany(
                "INSERT INTO leave_days (employee_id, date) VALUES (?, ?)",
                days,
            )
        db.execute(
            "UPDATE leave_requests SET status = ? WHERE id = ?",
            (status, request_id),
        )
        db.commit()
        employee_name = db.execute(
            "SELECT name FROM employees WHERE id = ?",
            (leave_request["employee_id"],),
        ).fetchone()["name"]
        write_activity(
            db,
            f"Leave request {status}",
            employee_name=employee_name,
            date=leave_request["start_date"],
            details=f"{leave_request['start_date']} through {leave_request['end_date']}.",
        )
        if status == "approved":
            rebalance_scheduled_future(date.fromisoformat(leave_request["start_date"]))
        return jsonify({"id": request_id, "status": status})

    @app.post("/api/leave")
    def add_leave():
        payload = request.get_json(silent=True) or {}
        parsed = parse_employee_date(payload)
        if not isinstance(parsed[1], date):
            return parsed
        employee_id, day = parsed
        active_count = get_db().execute(
            "SELECT COUNT(*) FROM employees WHERE active = 1"
        ).fetchone()[0]
        if active_count < MIN_ACTIVE_EMPLOYEES:
            return api_error(
                "Add employees until at least three are active before scheduling leave.",
                409,
            )
        employee = get_db().execute(
            "SELECT id, name FROM employees WHERE id = ? AND active = 1",
            (employee_id,),
        ).fetchone()
        if employee is None:
            return api_error("Choose an active employee.", 400)
        cursor = get_db().execute(
            "INSERT OR IGNORE INTO leave_days (employee_id, date) VALUES (?, ?)",
            (employee_id, day.isoformat()),
        )
        get_db().commit()
        if cursor.rowcount:
            write_activity(
                get_db(),
                "Leave marked",
                employee_name=employee["name"],
                date=day.isoformat(),
                details="Employee marked unavailable for this date.",
            )
        rebalance_scheduled_future(day)
        return jsonify({"ok": True})

    @app.delete("/api/leave")
    def remove_leave():
        payload = request.get_json(silent=True) or {}
        parsed = parse_employee_date(payload)
        if not isinstance(parsed[1], date):
            return parsed
        employee_id, day = parsed
        db = get_db()
        employee = db.execute(
            "SELECT name FROM employees WHERE id = ?", (employee_id,)
        ).fetchone()
        cursor = db.execute(
            "DELETE FROM leave_days WHERE employee_id = ? AND date = ?",
            (employee_id, day.isoformat()),
        )
        db.commit()
        if cursor.rowcount and employee is not None:
            write_activity(
                db,
                "Leave canceled",
                employee_name=employee["name"],
                date=day.isoformat(),
                details="Employee marked available for this date.",
            )
        rebalance_scheduled_future(day)
        return jsonify({"ok": True})

    @app.get("/api/attendance")
    def attendance_for_date():
        try:
            day = date.fromisoformat(request.args.get("date", ""))
        except ValueError:
            return api_error("Provide a valid date.", 400)
        if day.weekday() != 5:
            return api_error("Attendance can only be recorded for Saturdays.", 400)
        db = get_db()
        recorded = db.execute(
            "SELECT 1 FROM attendance_records WHERE date = ?", (day.isoformat(),)
        ).fetchone() is not None
        rows = db.execute(
            """
            SELECT e.id, e.name, e.active
            FROM attendance_employees a
            JOIN employees e ON e.id = a.employee_id
            WHERE a.date = ?
            ORDER BY e.name COLLATE NOCASE
            """,
            (day.isoformat(),),
        ).fetchall()
        return jsonify(
            {
                "recorded": recorded,
                "employee_ids": [row["id"] for row in rows],
                "employees": [
                    {"id": row["id"], "name": row["name"], "active": bool(row["active"])}
                    for row in rows
                ],
            }
        )

    @app.put("/api/attendance/<day_text>")
    def save_attendance(day_text: str):
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            return api_error("Provide a valid date.", 400)
        if day.weekday() != 5:
            return api_error("Attendance can only be recorded for Saturdays.", 400)
        payload = request.get_json(silent=True) or {}
        employee_ids = payload.get("employee_ids")
        if (
            not isinstance(employee_ids, list)
            or any(
                not isinstance(item, int) or isinstance(item, bool)
                for item in employee_ids
            )
            or len(set(employee_ids)) != len(employee_ids)
        ):
            return api_error("Provide a list of distinct employees who worked.", 400)

        db = get_db()
        if employee_ids:
            placeholders = ",".join("?" for _ in employee_ids)
            employees = db.execute(
                f"SELECT id FROM employees WHERE id IN ({placeholders})",
                employee_ids,
            ).fetchall()
            if len(employees) != len(employee_ids):
                return api_error("Choose employees from the roster.", 400)

        day_text = day.isoformat()
        db.execute("INSERT OR IGNORE INTO attendance_records (date) VALUES (?)", (day_text,))
        db.execute("DELETE FROM attendance_employees WHERE date = ?", (day_text,))
        db.executemany(
            "INSERT INTO attendance_employees (date, employee_id) VALUES (?, ?)",
            [(day_text, employee_id) for employee_id in employee_ids],
        )
        leave_canceled: list[int] = []
        if employee_ids:
            placeholders = ",".join("?" for _ in employee_ids)
            leave_canceled = [
                row["employee_id"]
                for row in db.execute(
                    f"""
                    SELECT employee_id FROM leave_days
                    WHERE date = ? AND employee_id IN ({placeholders})
                    """,
                    [day_text, *employee_ids],
                ).fetchall()
            ]
            db.execute(
                f"""
                DELETE FROM leave_days
                WHERE date = ? AND employee_id IN ({placeholders})
                """,
                [day_text, *employee_ids],
            )
        db.commit()
        if leave_canceled:
            rebalance_scheduled_future(day)
        attendee_names = []
        if employee_ids:
            placeholders = ",".join("?" for _ in employee_ids)
            attendee_names = db.execute(
                f"""
                SELECT name FROM employees
                WHERE id IN ({placeholders})
                ORDER BY name COLLATE NOCASE
                """,
                employee_ids,
            ).fetchall()
        write_activity(
            db,
            "Attendance recorded",
            date=day_text,
            details=(
                "Actually worked: "
                + (", ".join(row["name"] for row in attendee_names) or "none")
                + (
                    f"; leave canceled for {len(leave_canceled)} employee(s)."
                    if leave_canceled
                    else "."
                )
            ),
        )
        return jsonify(
            {"ok": True, "recorded": True, "leave_canceled": leave_canceled}
        )

    @app.post("/api/assignments/<day_text>")
    def override_assignment(day_text: str):
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            return api_error("Provide a valid date.", 400)
        if day.weekday() != 5:
            return api_error("Assignments can only be changed for Saturdays.", 400)
        active_count = get_db().execute(
            "SELECT COUNT(*) FROM employees WHERE active = 1"
        ).fetchone()[0]
        if active_count < MIN_ACTIVE_EMPLOYEES:
            return api_error(
                "At least three active employees are required before scheduling.",
                409,
            )
        payload = request.get_json(silent=True) or {}
        employee_ids = payload.get("employee_ids")
        if (
            not isinstance(employee_ids, list)
            or len(employee_ids) != 2
            or employee_ids[0] == employee_ids[1]
            or any(
                not isinstance(item, int) or isinstance(item, bool)
                for item in employee_ids
            )
        ):
            return api_error("Select exactly two different employees.", 400)
        placeholders = ",".join("?" for _ in employee_ids)
        employees = get_db().execute(
            f"SELECT id FROM employees WHERE active = 1 AND id IN ({placeholders})",
            employee_ids,
        ).fetchall()
        if len(employees) != 2:
            return api_error("Both employees must be active.", 400)
        on_leave = get_db().execute(
            f"""
            SELECT employee_id FROM leave_days
            WHERE date = ? AND employee_id IN ({placeholders})
            """,
            [day.isoformat(), *employee_ids],
        ).fetchone()
        if on_leave:
            return api_error("An employee on leave cannot be assigned.", 400)
        unavailable_weekday = get_db().execute(
            f"""
            SELECT employee_id FROM employee_unavailable_weekdays
            WHERE weekday = ? AND employee_id IN ({placeholders})
            """,
            [day.weekday(), *employee_ids],
        ).fetchone()
        if unavailable_weekday:
            return api_error(
                "An employee marked unavailable that weekday cannot be assigned.",
                400,
            )
        db = get_db()
        first_id, second_id = sorted(employee_ids)
        restricted_pair = db.execute(
            """
            SELECT 1 FROM employee_pair_exclusions
            WHERE employee1_id = ? AND employee2_id = ?
            """,
            (first_id, second_id),
        ).fetchone()
        if restricted_pair is not None:
            return api_error("These employees are restricted from working together.", 400)
        employee_names = db.execute(
            f"SELECT name FROM employees WHERE id IN ({placeholders}) ORDER BY id",
            employee_ids,
        ).fetchall()
        db.execute(
            """
            INSERT INTO assignments (date, employee1_id, employee2_id, manual)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(date) DO UPDATE SET
                employee1_id = excluded.employee1_id,
                employee2_id = excluded.employee2_id,
                manual = 1
            """,
            (day.isoformat(), employee_ids[0], employee_ids[1]),
        )
        db.commit()
        write_activity(
            db,
            "Saturday assignment overridden",
            date=day.isoformat(),
            details="Assigned: " + " + ".join(row["name"] for row in employee_names),
        )
        return jsonify({"ok": True})

    @app.delete("/api/assignments/<day_text>")
    def clear_assignment_override(day_text: str):
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            return api_error("Provide a valid date.", 400)
        if day.weekday() != 5:
            return api_error("Assignments can only be changed for Saturdays.", 400)
        if day < date.today():
            return api_error("Past assignments cannot be returned to automatic rotation.", 409)

        db = get_db()
        assignment = db.execute(
            "SELECT manual FROM assignments WHERE date = ?",
            (day.isoformat(),),
        ).fetchone()
        if assignment is None or not assignment["manual"]:
            return api_error("This Saturday has no manual override to clear.", 409)

        active_count = db.execute(
            "SELECT COUNT(*) FROM employees WHERE active = 1"
        ).fetchone()[0]
        if active_count < MIN_ACTIVE_EMPLOYEES:
            return api_error(
                "At least three active employees are required before scheduling.",
                409,
            )
        closed_holiday = db.execute(
            "SELECT dc_closed FROM holidays WHERE date = ?",
            (day.isoformat(),),
        ).fetchone()
        if closed_holiday is None or not closed_holiday["dc_closed"]:
            available_count = db.execute(
                """
                SELECT COUNT(*) FROM employees e
                WHERE e.active = 1
                  AND NOT EXISTS (
                      SELECT 1 FROM leave_days l
                      WHERE l.employee_id = e.id AND l.date = ?
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM employee_unavailable_weekdays w
                      WHERE w.employee_id = e.id AND w.weekday = ?
                  )
                """,
                (day.isoformat(), day.weekday()),
            ).fetchone()[0]
            if available_count < 2:
                return api_error(
                    "Fewer than two employees are available; the manual assignment was kept.",
                    409,
                )

        db.execute("DELETE FROM assignments WHERE date = ?", (day.isoformat(),))
        db.commit()
        ensure_assignments(day, day + timedelta(days=1))
        automatic_assignment = db.execute(
            """
            SELECT e1.name AS employee1, e2.name AS employee2
            FROM assignments a
            JOIN employees e1 ON e1.id = a.employee1_id
            JOIN employees e2 ON e2.id = a.employee2_id
            WHERE a.date = ? AND a.manual = 0
            """,
            (day.isoformat(),),
        ).fetchone()
        details = (
            "Automatic team: "
            + " + ".join(
                [automatic_assignment["employee1"], automatic_assignment["employee2"]]
            )
            if automatic_assignment is not None
            else "Manual assignment cleared; DC is closed on this Saturday."
        )
        write_activity(
            db,
            "Saturday override cleared",
            date=day.isoformat(),
            details=details,
        )
        return jsonify(
            {
                "ok": True,
                "manual": False,
                "employee_names": (
                    [automatic_assignment["employee1"], automatic_assignment["employee2"]]
                    if automatic_assignment is not None
                    else []
                ),
            }
        )

    @app.get("/api/substitutes/<day_text>")
    def recommend_substitutes(day_text: str):
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            return api_error("Provide a valid date.", 400)
        if day.weekday() != 5:
            return api_error("Substitute recommendations are only available for Saturdays.", 400)

        db = get_db()
        assignment = db.execute(
            """
            SELECT employee1_id, employee2_id
            FROM assignments WHERE date = ?
            """,
            (day.isoformat(),),
        ).fetchone()
        assigned_ids = (
            {assignment["employee1_id"], assignment["employee2_id"]}
            if assignment is not None
            else set()
        )
        month_start = day.replace(day=1).isoformat()
        previous_day = (day - timedelta(days=7)).isoformat()
        previous_assignment = db.execute(
            """
            SELECT employee1_id, employee2_id
            FROM assignments WHERE date = ?
            """,
            (previous_day,),
        ).fetchone()
        previous_ids = (
            {previous_assignment["employee1_id"], previous_assignment["employee2_id"]}
            if previous_assignment is not None
            else set()
        )
        counts = {
            row["employee_id"]: row["assignment_count"]
            for row in db.execute(
                """
                SELECT employee_id, COUNT(*) AS assignment_count
                FROM (
                    SELECT employee1_id AS employee_id
                    FROM assignments
                    WHERE date >= ? AND date < ?
                    UNION ALL
                    SELECT employee2_id AS employee_id
                    FROM assignments
                    WHERE date >= ? AND date < ?
                )
                GROUP BY employee_id
                """,
                (month_start, day.isoformat(), month_start, day.isoformat()),
            ).fetchall()
        }
        actual_work_counts = {
            row["employee_id"]: row["worked_count"]
            for row in db.execute(
                """
                SELECT employee_id, COUNT(*) AS worked_count
                FROM attendance_employees
                WHERE date >= ? AND date < ?
                GROUP BY employee_id
                """,
                (month_start, day.isoformat()),
            ).fetchall()
        }
        candidates = db.execute(
            """
            SELECT e.id, e.name
            FROM employees e
            WHERE e.active = 1
              AND NOT EXISTS (
                  SELECT 1 FROM leave_days l
                  WHERE l.employee_id = e.id AND l.date = ?
              )
              AND NOT EXISTS (
                  SELECT 1 FROM employee_unavailable_weekdays w
                  WHERE w.employee_id = e.id AND w.weekday = ?
              )
            """,
            (day.isoformat(), day.weekday()),
        ).fetchall()
        exclusion_rows = db.execute(
            """
            SELECT employee1_id, employee2_id
            FROM employee_pair_exclusions
            """
        ).fetchall()
        excluded_pairs = {
            (row["employee1_id"], row["employee2_id"])
            for row in exclusion_rows
        }
        recommendations = sorted(
            (
                {
                    "id": employee["id"],
                    "name": employee["name"],
                    "assignments_this_month": counts.get(employee["id"], 0),
                    "worked_this_month": actual_work_counts.get(employee["id"], 0),
                    "worked_previous_saturday": employee["id"] in previous_ids,
                    "incompatible_with_ids": sorted(
                        partner_id
                        for partner_id in assigned_ids
                        if tuple(sorted((employee["id"], partner_id))) in excluded_pairs
                    ),
                }
                for employee in candidates
                if employee["id"] not in assigned_ids
            ),
            key=lambda employee: (
                employee["assignments_this_month"],
                employee["worked_this_month"],
                employee["worked_previous_saturday"],
                employee["id"],
            ),
        )
        return jsonify(
            {
                "assigned_employee_ids": sorted(assigned_ids),
                "substitutes": recommendations[:3],
            }
        )

    @app.get("/api/export.csv")
    def export_csv():
        period = requested_period()
        if not isinstance(period[0], date):
            return period
        start, end = period
        active_count = get_db().execute(
            "SELECT COUNT(*) FROM employees WHERE active = 1"
        ).fetchone()[0]
        if active_count >= MIN_ACTIVE_EMPLOYEES:
            ensure_assignments(start, end)
            rows = get_db().execute(
                """
                SELECT a.date, e1.name AS employee1, e2.name AS employee2, a.manual
                FROM assignments a
                JOIN employees e1 ON e1.id = a.employee1_id
                JOIN employees e2 ON e2.id = a.employee2_id
                WHERE a.date >= ? AND a.date < ?
                ORDER BY a.date
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        else:
            rows = []
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            [
                "Date",
                "Employee 1",
                "Employee 2",
                "Actually worked",
                "Attendance recorded",
                "On leave",
                "Manual override",
                "Coverage",
            ]
        )
        row_by_date = {row["date"]: row for row in rows}
        leave_rows = get_db().execute(
            """
            SELECT l.date, e.name
            FROM leave_days l
            JOIN employees e ON e.id = l.employee_id
            WHERE l.date >= ? AND l.date < ?
            ORDER BY e.name COLLATE NOCASE
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        leave_by_date: dict[str, list[str]] = {}
        for leave in leave_rows:
            leave_by_date.setdefault(leave["date"], []).append(leave["name"])
        attendance_dates = {
            row["date"]
            for row in get_db().execute(
                "SELECT date FROM attendance_records WHERE date >= ? AND date < ?",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        }
        attendance_rows = get_db().execute(
            """
            SELECT a.date, e.name
            FROM attendance_employees a
            JOIN employees e ON e.id = a.employee_id
            WHERE a.date >= ? AND a.date < ?
            ORDER BY e.name COLLATE NOCASE
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        attendance_by_date: dict[str, list[str]] = {}
        for attendance in attendance_rows:
            attendance_by_date.setdefault(attendance["date"], []).append(attendance["name"])
        gaps = {gap["date"]: gap["available"] for gap in coverage_gaps(start, end)}
        holidays = {
            row["date"]: row
            for row in get_db().execute(
                "SELECT date, name, dc_closed FROM holidays WHERE date >= ? AND date < ?",
                (start.isoformat(), end.isoformat()),
            )
        }
        current = start + timedelta(days=(5 - start.weekday()) % 7)
        while current < end:
            day = current.isoformat()
            row = row_by_date.get(day)
            holiday = holidays.get(day)
            if holiday is not None:
                coverage = (
                    f'DC closed: {holiday["name"] or "Holiday"}'
                    if holiday["dc_closed"]
                    else f'DC open: {holiday["name"] or "Holiday"}'
                )
            elif active_count < MIN_ACTIVE_EMPLOYEES:
                coverage = f"Roster below {MIN_ACTIVE_EMPLOYEES}-employee minimum"
            elif day in gaps:
                coverage = f'{gaps[day]} available'
            else:
                coverage = "Covered"
            writer.writerow(
                [
                    day,
                    row["employee1"] if row else "",
                    row["employee2"] if row else "",
                    "; ".join(attendance_by_date.get(day, [])),
                    "Yes" if day in attendance_dates else "No",
                    "; ".join(leave_by_date.get(day, [])),
                    "Yes" if row and row["manual"] else "No",
                    coverage,
                ]
            )
            current += timedelta(days=7)
        output.seek(0)
        return send_file(
            io.BytesIO(output.getvalue().encode("utf-8-sig")),
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"roster-{start:%Y-%m}.csv",
        )

    @app.errorhandler(sqlite3.Error)
    def handle_database_error(error: sqlite3.Error):
        app.logger.exception("Database operation failed", exc_info=error)
        return jsonify({"error": "A database error occurred."}), 500

    @app.errorhandler(RequestEntityTooLarge)
    def handle_upload_too_large(_error: RequestEntityTooLarge):
        return api_error("Backup files must be 64 MB or smaller.", 413)

    return app


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db = sqlite3.connect(current_app.config["DATABASE"])
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        g.db = db
    return g.db


def write_activity(
    db: sqlite3.Connection,
    action: str,
    *,
    actor: str | None = None,
    employee_name: str = "",
    date: str = "",
    details: str = "",
) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            actor TEXT NOT NULL DEFAULT 'Local operator',
            action TEXT NOT NULL,
            employee_name TEXT NOT NULL DEFAULT '',
            date TEXT NOT NULL DEFAULT '',
            details TEXT NOT NULL DEFAULT ''
        )
        """
    )
    db.execute(
        """
        INSERT INTO activity_log (actor, action, employee_name, date, details)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            actor
            or (
                g.user["username"]
                if has_request_context() and getattr(g, "user", None)
                else "Local operator"
            ),
            action,
            employee_name,
            date,
            details,
        ),
    )
    db.commit()


def api_error(message: str, status: int):
    return jsonify({"error": message}), status


def requested_period() -> tuple[date, date] | tuple[Any, int]:
    try:
        start = date.fromisoformat(request.args.get("start", "")[:10])
        end = date.fromisoformat(request.args.get("end", "")[:10])
    except ValueError:
        return api_error("Provide valid start and end dates.", 400)
    if end <= start or (end - start).days > 370:
        return api_error("The date range must be between 1 and 370 days.", 400)
    return start, end


def parse_employee_date(payload: dict[str, Any]) -> tuple[int, date] | tuple[Any, int]:
    employee_id = payload.get("employee_id")
    try:
        day = date.fromisoformat(payload.get("date", ""))
    except (TypeError, ValueError):
        return api_error("Provide a valid date.", 400)
    if not isinstance(employee_id, int) or isinstance(employee_id, bool):
        return api_error("Provide a valid employee.", 400)
    return employee_id, day


def ensure_assignments(start: date, end: date) -> None:
    db = get_db()
    employees = db.execute(
        "SELECT id FROM employees WHERE active = 1 ORDER BY id"
    ).fetchall()
    if len(employees) < MIN_ACTIVE_EMPLOYEES:
        return

    active_ids = {employee["id"] for employee in employees}
    excluded_pairs = {
        (row["employee1_id"], row["employee2_id"])
        for row in db.execute(
            "SELECT employee1_id, employee2_id FROM employee_pair_exclusions"
        ).fetchall()
    }
    current = start + timedelta(days=(5 - start.weekday()) % 7)
    current_month: tuple[int, int] | None = None
    month_counts: dict[int, int] = {}
    actual_work_counts: dict[int, int] = {}
    previous_saturday_ids: set[int] = set()
    previous_date = (current - timedelta(days=7)).isoformat()
    previous_assignment = db.execute(
        """
        SELECT employee1_id, employee2_id
        FROM assignments
        WHERE date = ?
        """,
        (previous_date,),
    ).fetchone()
    if previous_assignment is not None:
        previous_saturday_ids = {
            previous_assignment["employee1_id"],
            previous_assignment["employee2_id"],
        }
    while current < end:
        month = (current.year, current.month)
        if month != current_month:
            current_month = month
            month_counts = {employee_id: 0 for employee_id in active_ids}
            month_start = current.replace(day=1).isoformat()
            prior_assignments = db.execute(
                """
                SELECT date, employee1_id, employee2_id, manual
                FROM assignments
                WHERE date >= ? AND date < ?
                """,
                (month_start, current.isoformat()),
            ).fetchall()
            for prior in prior_assignments:
                for employee_id in (prior["employee1_id"], prior["employee2_id"]):
                    if employee_id in month_counts:
                        month_counts[employee_id] += 1
            actual_work_counts = {employee_id: 0 for employee_id in active_ids}
            actual_rows = db.execute(
                """
                SELECT employee_id, COUNT(*) AS worked_count
                FROM attendance_employees
                WHERE date >= ? AND date < ?
                GROUP BY employee_id
                """,
                (month_start, current.isoformat()),
            ).fetchall()
            for actual in actual_rows:
                if actual["employee_id"] in actual_work_counts:
                    actual_work_counts[actual["employee_id"]] = actual["worked_count"]
        month_start_date = current.replace(day=1)
        days_in_month = calendar.monthrange(current.year, current.month)[1]
        first_saturday = 1 + (5 - month_start_date.weekday()) % 7
        saturdays_in_month = (
            0
            if first_saturday > days_in_month
            else 1 + (days_in_month - first_saturday) // 7
        )
        assignment_limit = max(0, saturdays_in_month - MIN_SATURDAYS_OFF)

        current_text = current.isoformat()
        holiday = db.execute(
            "SELECT dc_closed FROM holidays WHERE date = ?", (current_text,)
        ).fetchone()
        if holiday is not None and holiday["dc_closed"] and current >= date.today():
            db.execute("DELETE FROM assignments WHERE date = ?", (current_text,))
            previous_saturday_ids = set()
            current += timedelta(days=7)
            continue
        existing = db.execute(
            "SELECT employee1_id, employee2_id, manual FROM assignments WHERE date = ?",
            (current_text,),
        ).fetchone()
        is_historical = current < date.today()
        unavailable = {
            row["employee_id"]
            for row in db.execute(
                """
                SELECT employee_id FROM leave_days WHERE date = ?
                UNION
                SELECT employee_id FROM employee_unavailable_weekdays WHERE weekday = ?
                """,
                (current_text, current.weekday()),
            )
        }
        if (
            existing is not None
            and existing["employee1_id"] != existing["employee2_id"]
            and tuple(
                sorted((existing["employee1_id"], existing["employee2_id"]))
            ) not in excluded_pairs
            and (
                is_historical
                or (
                    existing["employee1_id"] in active_ids
                    and existing["employee2_id"] in active_ids
                    and existing["employee1_id"] not in unavailable
                    and existing["employee2_id"] not in unavailable
                    and existing["manual"]
                )
            )
        ):
            assigned = [existing["employee1_id"], existing["employee2_id"]]
        else:
            available = [employee["id"] for employee in employees if employee["id"] not in unavailable]
            recent_start = (current - timedelta(days=90)).isoformat()
            recent_counts = {employee_id: 0 for employee_id in active_ids}
            recent_assignments = db.execute(
                """
                SELECT employee1_id, employee2_id
                FROM assignments
                WHERE date >= ? AND date < ?
                """,
                (recent_start, current_text),
            ).fetchall()
            for prior in recent_assignments:
                for employee_id in (prior["employee1_id"], prior["employee2_id"]):
                    if employee_id in recent_counts:
                        recent_counts[employee_id] += 1
            scores = {
                employee_id: (
                    month_counts.get(employee_id, 0),
                    recent_counts.get(employee_id, 0),
                    actual_work_counts.get(employee_id, 0),
                    employee_id in previous_saturday_ids,
                    employee_id,
                )
                for employee_id in available
            }
            feasible_pairs = [
                pair
                for pair in combinations(available, 2)
                if tuple(sorted(pair)) not in excluded_pairs
            ]
            if not feasible_pairs:
                db.execute("DELETE FROM assignments WHERE date = ?", (current_text,))
                previous_saturday_ids = set()
                current += timedelta(days=7)
                continue

            def pair_priority(pair: tuple[int, int]) -> tuple[int, int, tuple[tuple[int, ...], ...]]:
                projected_counts = {
                    employee_id: month_counts.get(employee_id, 0) + 1
                    for employee_id in pair
                }
                off_target_gap = sum(
                    max(
                        0,
                        MIN_SATURDAYS_OFF
                        - (saturdays_in_month - projected_counts[employee_id]),
                    )
                    for employee_id in pair
                )
                excess_after_assignment = sum(
                    max(
                        0,
                        projected_counts[employee_id] - assignment_limit,
                    )
                    for employee_id in pair
                )
                return (
                    excess_after_assignment,
                    off_target_gap,
                    tuple(sorted((scores[pair[0]], scores[pair[1]]))),
                )

            assigned = list(min(feasible_pairs, key=pair_priority))
            assigned.sort(key=lambda employee_id: scores[employee_id])
            db.execute(
                """
                INSERT INTO assignments (date, employee1_id, employee2_id, manual)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(date) DO UPDATE SET
                    employee1_id = excluded.employee1_id,
                    employee2_id = excluded.employee2_id,
                    manual = 0
                """,
                (current_text, assigned[0], assigned[1]),
            )
        for employee_id in assigned:
            month_counts[employee_id] = month_counts.get(employee_id, 0) + 1
        previous_saturday_ids = set(assigned)
        current += timedelta(days=7)
    db.commit()


def rebalance_scheduled_future(start: date | None = None) -> None:
    first_day = max(date.today(), start or date.today())
    if first_day.weekday() == 5:
        ensure_assignments(first_day, first_day + timedelta(days=1))
    scheduled_dates = get_db().execute(
        "SELECT date FROM assignments WHERE date >= ? ORDER BY date",
        (first_day.isoformat(),),
    ).fetchall()
    for row in scheduled_dates:
        scheduled_day = date.fromisoformat(row["date"])
        ensure_assignments(scheduled_day, scheduled_day + timedelta(days=1))


def coverage_gaps(start: date, end: date) -> list[dict[str, int | str]]:
    db = get_db()
    gaps = []
    current = start + timedelta(days=(5 - start.weekday()) % 7)
    while current < end:
        holiday = db.execute(
            "SELECT dc_closed FROM holidays WHERE date = ?", (current.isoformat(),)
        ).fetchone()
        if holiday is not None and holiday["dc_closed"]:
            current += timedelta(days=7)
            continue
        available = db.execute(
            """
            SELECT e.id FROM employees e
            WHERE e.active = 1
              AND NOT EXISTS (
                  SELECT 1 FROM leave_days l
                  WHERE l.employee_id = e.id AND l.date = ?
              )
              AND NOT EXISTS (
                  SELECT 1 FROM employee_unavailable_weekdays w
                  WHERE w.employee_id = e.id AND w.weekday = ?
              )
            """,
            (current.isoformat(), current.weekday()),
        ).fetchall()
        available_ids = [row["id"] for row in available]
        restricted_pairs = {
            (row["employee1_id"], row["employee2_id"])
            for row in db.execute(
                "SELECT employee1_id, employee2_id FROM employee_pair_exclusions"
            ).fetchall()
        }
        feasible_pair_exists = any(
            tuple(sorted(pair)) not in restricted_pairs
            for pair in combinations(available_ids, 2)
        )
        if len(available_ids) < 2 or not feasible_pair_exists:
            gaps.append(
                {"date": current.isoformat(), "available": len(available_ids)}
            )
        current += timedelta(days=7)
    return gaps


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
