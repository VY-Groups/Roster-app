import csv
import io
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import create_app, get_db, write_activity


class RosterAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database = str(Path(self.temp_dir.name) / "roster.db")
        self.app = create_app({"TESTING": True, "DATABASE": database})
        self.client = self.app.test_client()
        self.sign_up_admin(self.client)
        self.employees = []
        for name in ("Avery", "Bailey", "Casey", "Devon"):
            response = self.client.post("/api/employees", json={"name": name})
            self.assertEqual(response.status_code, 201)
            self.employees.append(response.get_json())

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def sign_up_admin(client):
        client.get("/setup")
        with client.session_transaction() as current_session:
            setup_token = current_session["csrf_token"]
        return client.post(
            "/setup",
            data={
                "username": "admin",
                "password": "roster-admin-test-password",
                "csrf_token": setup_token,
            },
        )

    @staticmethod
    def sign_in(client, username="admin", password="roster-admin-test-password"):
        client.get("/login")
        with client.session_transaction() as current_session:
            token = current_session["csrf_token"]
        return client.post(
            "/login",
            data={
                "username": username,
                "password": password,
                "csrf_token": token,
            },
        )

    def test_roster_requires_sign_in_and_first_admin_setup(self):
        unauthenticated = self.app.test_client()

        api_response = unauthenticated.get("/api/employees")
        page_response = unauthenticated.get("/")

        self.assertEqual(api_response.status_code, 401)
        self.assertEqual(page_response.status_code, 302)
        self.assertIn("/login", page_response.location)

    def test_login_rejects_invalid_password(self):
        login_client = self.app.test_client()
        login_client.get("/login")
        with login_client.session_transaction() as session:
            token = session["csrf_token"]

        response = login_client.post(
            "/login",
            data={
                "username": "admin",
                "password": "incorrect-password",
                "csrf_token": token,
            },
        )

        self.assertEqual(response.status_code, 401)
        self.assertIn(b"Username or password is incorrect", response.data)

    def test_viewer_can_read_but_cannot_mutate_roster(self):
        created = self.client.post(
            "/api/users",
            json={
                "username": "reader",
                "password": "reader-test-password",
                "role": "viewer",
            },
        )
        viewer = self.app.test_client()
        viewer.get("/login")
        with viewer.session_transaction() as session:
            token = session["csrf_token"]
        signed_in = viewer.post(
            "/login",
            data={
                "username": "reader",
                "password": "reader-test-password",
                "csrf_token": token,
            },
        )
        read_response = viewer.get("/api/employees")
        write_response = viewer.post("/api/employees", json={"name": "Blocked"})

        self.assertEqual(created.status_code, 201)
        self.assertEqual(signed_in.status_code, 302)
        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(write_response.status_code, 403)

    def test_mutating_api_requires_csrf_header(self):
        response = self.client.post(
            "/api/employees",
            json={"name": "No CSRF"},
            headers={"X-CSRF-Token": "wrong"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("security token", response.get_json()["error"].lower())

    def test_logout_clears_authenticated_session(self):
        with self.client.session_transaction() as session:
            token = session["csrf_token"]

        logout = self.client.post(
            "/logout",
            data={"csrf_token": token},
        )
        protected = self.client.get("/api/employees")

        self.assertEqual(logout.status_code, 302)
        self.assertEqual(protected.status_code, 401)

    def test_account_endpoints_reject_invalid_role_shapes_and_short_passwords(self):
        bad_role = self.client.post(
            "/api/users",
            json={
                "username": "invalid-role",
                "password": "password-long-enough",
                "role": [],
            },
        )
        short_password = self.client.post(
            "/api/users",
            json={
                "username": "short-password",
                "password": "short",
                "role": "viewer",
            },
        )

        self.assertEqual(bad_role.status_code, 400)
        self.assertEqual(short_password.status_code, 400)

    @staticmethod
    def next_saturday():
        today = date.today()
        days_ahead = (5 - today.weekday()) % 7 or 7
        return today + timedelta(days=days_ahead)

    @staticmethod
    def day_after(day):
        return (date.fromisoformat(day) + timedelta(days=1)).isoformat()

    def test_calendar_generates_two_distinct_employees_per_saturday(self):
        events = self.client.get(
            "/api/calendar?start=2026-10-01&end=2026-11-01"
        ).get_json()

        self.assertEqual([event["start"] for event in events], [
            "2026-10-03", "2026-10-10", "2026-10-17", "2026-10-24", "2026-10-31"
        ])
        for event in events:
            self.assertEqual(len(event["extendedProps"]["employee_ids"]), 2)
            self.assertNotEqual(*event["extendedProps"]["employee_ids"])

    def test_rotation_avoids_consecutive_saturdays_when_workload_is_tied(self):
        events = self.client.get(
            "/api/calendar?start=2026-10-01&end=2026-11-01"
        ).get_json()
        assigned_sets = [
            set(event["extendedProps"]["employee_ids"])
            for event in events
        ]

        self.assertEqual(len(assigned_sets), 5)
        for earlier, later in zip(assigned_sets, assigned_sets[1:]):
            self.assertFalse(earlier & later)

    def test_rotation_avoids_previous_manual_team_on_next_automatic_saturday(self):
        self.client.post(
            "/api/assignments/2026-10-03",
            json={
                "employee_ids": [
                    self.employees[0]["id"],
                    self.employees[1]["id"],
                ]
            },
        )

        events = self.client.get(
            "/api/calendar?start=2026-10-01&end=2026-10-18"
        ).get_json()

        previous_team = set(events[0]["extendedProps"]["employee_ids"])
        next_team = set(events[1]["extendedProps"]["employee_ids"])
        self.assertTrue(events[0]["extendedProps"]["manual"])
        self.assertFalse(events[1]["extendedProps"]["manual"])
        self.assertFalse(previous_team & next_team)

    def test_later_calendar_request_uses_prior_assignment_counts(self):
        self.client.get("/api/calendar?start=2026-10-01&end=2026-10-11")

        response = self.client.get(
            "/api/calendar?start=2026-10-10&end=2026-10-11"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_json()), 1)

    def test_employee_csv_import_adds_reactivates_and_skips_duplicates(self):
        inactive_employee = self.employees[-1]
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE employees SET active = 0 WHERE id = ?",
                (inactive_employee["id"],),
            )
            db.commit()
        csv_data = "Name\r\nNew Hire\r\nAvery\r\nnew hire\r\nDevon\r\n"

        response = self.client.post(
            "/api/employees/import",
            data={"file": (io.BytesIO(csv_data.encode("utf-8")), "staff.csv")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["added"], ["New Hire"])
        self.assertEqual(result["reactivated"], ["Devon"])
        self.assertEqual(result["skipped"], ["new hire", "Avery"])
        self.assertEqual(
            {
                employee["name"]
                for employee in self.client.get("/api/employees").get_json()
            },
            {"Avery", "Bailey", "Casey", "Devon", "New Hire"},
        )
        activity = self.client.get("/api/activity").get_json()["entries"]
        self.assertEqual(activity[0]["action"], "Employees imported")
        self.assertIn("Added 1, reactivated 1, skipped 2", activity[0]["details"])

    def test_employee_csv_import_rejects_invalid_file_without_partial_changes(self):
        before_names = {
            employee["name"] for employee in self.client.get("/api/employees").get_json()
        }
        before_activity_count = len(
            self.client.get("/api/activity").get_json()["entries"]
        )
        invalid_csv = "Name\nValid New Name\n" + ("X" * 81) + "\n"

        invalid_name = self.client.post(
            "/api/employees/import",
            data={"file": (io.BytesIO(invalid_csv.encode("utf-8")), "staff.csv")},
            content_type="multipart/form-data",
        )
        wrong_extension = self.client.post(
            "/api/employees/import",
            data={"file": (io.BytesIO(b"Name\nOther\n"), "staff.txt")},
            content_type="multipart/form-data",
        )
        bad_encoding = self.client.post(
            "/api/employees/import",
            data={"file": (io.BytesIO(b"\xff\xfe"), "staff.csv")},
            content_type="multipart/form-data",
        )

        self.assertEqual(invalid_name.status_code, 400)
        self.assertIn("row 3", invalid_name.get_json()["error"].lower())
        self.assertEqual(wrong_extension.status_code, 400)
        self.assertEqual(bad_encoding.status_code, 400)
        self.assertEqual(
            {
                employee["name"]
                for employee in self.client.get("/api/employees").get_json()
            },
            before_names,
        )
        self.assertEqual(
            len(self.client.get("/api/activity").get_json()["entries"]),
            before_activity_count,
        )

    def test_employee_csv_import_template_is_downloadable(self):
        response = self.client.get("/api/employees/template.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual(
            list(csv.reader(io.StringIO(response.data.decode("utf-8-sig")))),
            [["Name"], ["Avery"]],
        )

    def test_employee_roster_export_downloads_current_snapshot(self):
        response = self.client.get("/api/employees/export.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))
        self.assertEqual(rows[0], ["ID", "Name", "Active"])
        self.assertEqual(
            [row[1] for row in rows[1:]],
            ["Avery", "Bailey", "Casey", "Devon"],
        )

    def test_leave_csv_import_adds_rows_skips_duplicates_and_rebalances(self):
        self.client.post(
            "/api/leave",
            json={"employee_id": self.employees[2]["id"], "date": "2027-10-09"},
        )
        csv_data = (
            "Employee,Date\r\n"
            "Avery,2027-10-02\r\n"
            "Bailey,2027-10-02\r\n"
            "avery,2027-10-02\r\n"
            "Casey,2027-10-09\r\n"
        )

        response = self.client.post(
            "/api/leave/import",
            data={"file": (io.BytesIO(csv_data.encode("utf-8")), "leave.csv")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(
            result["added"],
            ["Avery - 2027-10-02", "Bailey - 2027-10-02"],
        )
        self.assertEqual(
            result["skipped"],
            ["Avery - 2027-10-02", "Casey - 2027-10-09"],
        )
        self.assertEqual(
            self.client.get("/api/leave?date=2027-10-02").get_json()["employee_ids"],
            [self.employees[0]["id"], self.employees[1]["id"]],
        )
        event = self.client.get(
            "/api/calendar?start=2027-10-02&end=2027-10-03"
        ).get_json()[0]
        self.assertEqual(
            set(event["extendedProps"]["employee_ids"]),
            {self.employees[2]["id"], self.employees[3]["id"]},
        )
        activity = self.client.get("/api/activity").get_json()["entries"][0]
        self.assertEqual(activity["action"], "Leave imported")
        self.assertIn("Added 2 leave day(s)", activity["details"])

    def test_leave_csv_import_rejects_invalid_row_without_partial_changes(self):
        csv_data = "Employee,Date\r\nAvery,2027-10-02\r\nUnknown,2027-10-09\r\n"
        before_activity_count = len(
            self.client.get("/api/activity").get_json()["entries"]
        )

        response = self.client.post(
            "/api/leave/import",
            data={"file": (io.BytesIO(csv_data.encode("utf-8")), "leave.csv")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("row 3", response.get_json()["error"].lower())
        self.assertEqual(
            self.client.get("/api/leave?date=2027-10-02").get_json()["employee_ids"],
            [],
        )
        self.assertEqual(
            len(self.client.get("/api/activity").get_json()["entries"]),
            before_activity_count,
        )

    def test_leave_csv_import_rejects_pending_request_overlap_atomically(self):
        self.client.post(
            "/api/leave-requests",
            json={
                "employee_id": self.employees[0]["id"],
                "start_date": "2027-10-02",
                "end_date": "2027-10-02",
            },
        )
        csv_data = "Employee,Date\r\nBailey,2027-10-02\r\nAvery,2027-10-02\r\n"

        response = self.client.post(
            "/api/leave/import",
            data={"file": (io.BytesIO(csv_data.encode("utf-8")), "leave.csv")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            self.client.get("/api/leave?date=2027-10-02").get_json()["employee_ids"],
            [],
        )

    def test_leave_csv_import_template_is_downloadable(self):
        response = self.client.get("/api/leave/template.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual(
            list(csv.reader(io.StringIO(response.data.decode("utf-8-sig")))),
            [["Employee", "Date"], ["Avery", "2026-10-10"]],
        )

    def test_activity_history_records_roster_mutations_newest_first(self):
        day = self.next_saturday().isoformat()
        renamed = self.client.patch(
            f'/api/employees/{self.employees[0]["id"]}',
            json={"name": "Avery Renamed"},
        )
        holiday = self.client.put(
            f"/api/holidays/{day}",
            json={"name": "Test holiday", "dc_closed": False},
        )
        assignment = self.client.post(
            f"/api/assignments/{day}",
            json={
                "employee_ids": [
                    self.employees[1]["id"],
                    self.employees[2]["id"],
                ]
            },
        )
        attendance = self.client.put(
            f"/api/attendance/{day}",
            json={"employee_ids": [self.employees[0]["id"], self.employees[3]["id"]]},
        )
        history = self.client.get("/api/activity").get_json()["entries"]
        exported = self.client.get("/api/activity.csv")
        exported_rows = list(csv.reader(io.StringIO(exported.data.decode("utf-8-sig"))))

        self.assertEqual(
            [renamed.status_code, holiday.status_code, assignment.status_code, attendance.status_code],
            [200, 200, 200, 200],
        )
        self.assertEqual(history[0]["action"], "Attendance recorded")
        self.assertEqual(history[0]["date"], day)
        self.assertIn("Avery Renamed", history[0]["details"])
        self.assertEqual(history[0]["actor"], "admin")
        actions = {entry["action"] for entry in history}
        self.assertTrue(
            {
                "Employee added",
                "Employee renamed",
                "Holiday added",
                "Saturday assignment overridden",
                "Attendance recorded",
            } <= actions
        )
        self.assertEqual(exported.status_code, 200)
        self.assertIn("attachment", exported.headers["Content-Disposition"])
        self.assertEqual(
            exported_rows[0],
            ["ID", "Timestamp", "Operator", "Action", "Employee", "Date", "Details"],
        )
        self.assertEqual(exported_rows[-1][3], "Attendance recorded")
        self.assertEqual(len(exported_rows), len(history) + 1)

    def test_activity_history_supports_search_date_filter_and_pagination(self):
        with self.app.app_context():
            db = get_db()
            for index in range(45):
                write_activity(
                    db,
                    "Batch action",
                    employee_name=f"Batch Employee {index:02}",
                    date="2026-10-01" if index % 2 == 0 else "2026-10-02",
                    details=f"Batch row {index:02}",
                )

        first_page = self.client.get("/api/activity?page=1&page_size=10").get_json()
        second_page = self.client.get("/api/activity?page=2&page_size=10").get_json()
        filtered = self.client.get(
            "/api/activity?search=Batch%20Employee%2004&date=2026-10-01"
        ).get_json()
        beyond_last_page = self.client.get(
            "/api/activity?page=999&page_size=10"
        ).get_json()

        self.assertEqual(first_page["total"], 45 + 5)
        self.assertEqual(first_page["page"], 1)
        self.assertEqual(len(first_page["entries"]), 10)
        self.assertEqual(first_page["entries"][0]["employee_name"], "Batch Employee 44")
        self.assertEqual(second_page["page"], 2)
        self.assertTrue(
            set(entry["id"] for entry in first_page["entries"]).isdisjoint(
                entry["id"] for entry in second_page["entries"]
            )
        )
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["entries"][0]["employee_name"], "Batch Employee 04")
        self.assertEqual(beyond_last_page["page"], beyond_last_page["total_pages"])
        self.assertEqual(
            self.client.get("/api/activity?page_size=0").status_code,
            400,
        )
        self.assertEqual(
            self.client.get("/api/activity?date=not-a-date").status_code,
            400,
        )

    def test_activity_history_captures_leave_and_request_review(self):
        employee_id = self.employees[0]["id"]
        day = self.next_saturday().isoformat()
        marked = self.client.post(
            "/api/leave", json={"employee_id": employee_id, "date": day}
        )
        canceled = self.client.delete(
            "/api/leave", json={"employee_id": employee_id, "date": day}
        )
        leave_request = self.client.post(
            "/api/leave-requests",
            json={
                "employee_id": employee_id,
                "start_date": "2027-10-02",
                "end_date": "2027-10-10",
            },
        )
        reviewed = self.client.patch(
            f'/api/leave-requests/{leave_request.get_json()["id"]}',
            json={"status": "rejected"},
        )
        history = self.client.get("/api/activity").get_json()["entries"]
        actions = [entry["action"] for entry in history]

        self.assertEqual([marked.status_code, canceled.status_code], [200, 200])
        self.assertEqual(leave_request.status_code, 201)
        self.assertEqual(reviewed.status_code, 200)
        self.assertLess(actions.index("Leave canceled"), actions.index("Leave marked"))
        self.assertIn("Leave request submitted", actions)
        self.assertIn("Leave request rejected", actions)

    def test_leave_what_if_preview_rebalances_without_changing_saved_data(self):
        day = self.next_saturday().isoformat()
        selected_employee_id = self.employees[0]["id"]
        other_employee_id = self.employees[1]["id"]
        self.assertEqual(
            self.client.post(
                f"/api/assignments/{day}",
                json={"employee_ids": [selected_employee_id, other_employee_id]},
            ).status_code,
            200,
        )
        with self.app.app_context():
            db = get_db()
            saved_assignment = tuple(
                db.execute(
                    "SELECT employee1_id, employee2_id, manual FROM assignments WHERE date = ?",
                    (day,),
                ).fetchone()
            )
            saved_leave_count = db.execute(
                "SELECT COUNT(*) FROM leave_days"
            ).fetchone()[0]
            saved_activity_count = db.execute(
                "SELECT COUNT(*) FROM activity_log"
            ).fetchone()[0]

        response = self.client.get(
            f"/api/simulate/leave?employee_id={selected_employee_id}&date={day}"
        )

        self.assertEqual(response.status_code, 200)
        preview = response.get_json()
        self.assertTrue(preview["coverage_met"])
        self.assertIn("Two employees", preview["message"])
        self.assertIn("Avery", preview["scheduled_before"])
        self.assertNotIn("Avery", preview["scheduled_after"])
        self.assertEqual(len(preview["scheduled_after"]), 2)
        self.assertEqual(len(preview["workload"]), len(self.employees))
        self.assertTrue(
            all(
                isinstance(employee["scheduled_before"], int)
                and isinstance(employee["scheduled_after"], int)
                for employee in preview["workload"]
            )
        )
        with self.app.app_context():
            db = get_db()
            self.assertEqual(
                tuple(
                    db.execute(
                        "SELECT employee1_id, employee2_id, manual FROM assignments WHERE date = ?",
                        (day,),
                    ).fetchone()
                ),
                saved_assignment,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM leave_days").fetchone()[0],
                saved_leave_count,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0],
                saved_activity_count,
            )

    def test_leave_what_if_preview_rejects_invalid_dates_and_employees(self):
        employee_id = self.employees[0]["id"]
        weekday_response = self.client.get(
            f"/api/simulate/leave?employee_id={employee_id}&date=2026-10-08"
        )
        invalid_employee = self.client.get(
            f"/api/simulate/leave?employee_id=9999&date={self.next_saturday().isoformat()}"
        )
        self.assertEqual(weekday_response.status_code, 400)
        self.assertEqual(invalid_employee.status_code, 400)

    def test_roster_preview_adds_employee_without_persisting_changes(self):
        day = self.next_saturday().isoformat()
        with self.app.app_context():
            db = get_db()
            active_before = db.execute(
                "SELECT COUNT(*) FROM employees WHERE active = 1"
            ).fetchone()[0]
            assignments_before = db.execute(
                "SELECT COUNT(*) FROM assignments"
            ).fetchone()[0]
            activity_before = db.execute(
                "SELECT COUNT(*) FROM activity_log"
            ).fetchone()[0]

        response = self.client.get(
            f"/api/simulate/roster?action=add&name=Hypothetical%20Hire&date={day}"
        )

        self.assertEqual(response.status_code, 200)
        preview = response.get_json()
        self.assertEqual(preview["active_employee_count"], active_before + 1)
        self.assertEqual(
            len(preview["workload"]),
            active_before + 1,
        )
        self.assertEqual(
            date.fromisoformat(preview["horizon_end"])
            - date.fromisoformat(preview["horizon_start"]),
            timedelta(days=90),
        )
        self.assertEqual(len(preview["assignments_before"]), 13)
        self.assertEqual(len(preview["assignments_after"]), 13)
        self.assertTrue(any(item["name"] == "Hypothetical Hire" for item in preview["workload"]))
        self.assertEqual(
            [item["date"] for item in preview["assignments_before"]],
            [item["date"] for item in preview["assignments_after"]],
        )
        with self.app.app_context():
            db = get_db()
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM employees WHERE active = 1").fetchone()[0],
                active_before,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM assignments").fetchone()[0],
                assignments_before,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0],
                activity_before,
            )

    def test_roster_preview_removes_employee_and_reports_minimum_staffing_gap(self):
        day = self.next_saturday().isoformat()
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE employees SET active = 0 WHERE id = ?",
                (self.employees[-1]["id"],),
            )
            db.commit()
        with self.app.app_context():
            db = get_db()
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM employees WHERE active = 1").fetchone()[0],
                3,
            )
            assignment_count_before = db.execute(
                "SELECT COUNT(*) FROM assignments"
            ).fetchone()[0]
            activity_count_before = db.execute(
                "SELECT COUNT(*) FROM activity_log"
            ).fetchone()[0]

        response = self.client.get(
            f"/api/simulate/roster?action=remove&employee_id={self.employees[0]['id']}&date={day}"
        )

        self.assertEqual(response.status_code, 200)
        preview = response.get_json()
        self.assertEqual(preview["active_employee_count"], 2)
        self.assertTrue(preview["coverage_gaps"])
        self.assertGreater(len(preview["coverage_gaps"]), 1)
        self.assertEqual(len(preview["coverage_gaps"]), 13)
        self.assertEqual(preview["assignments_after"], [])
        self.assertTrue(
            any(
                item["name"] == "Avery" and not item["active_after"]
                for item in preview["workload"]
            )
        )
        with self.app.app_context():
            db = get_db()
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM employees WHERE active = 1").fetchone()[0],
                3,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM assignments").fetchone()[0],
                assignment_count_before,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0],
                activity_count_before,
            )

    def test_leave_reassigns_saturday_and_never_assigns_employee_on_leave(self):
        day = self.next_saturday().isoformat()
        before = self.client.get(
            f"/api/calendar?start={day}&end={self.day_after(day)}"
        ).get_json()[0]["extendedProps"]["employee_ids"]

        response = self.client.post(
            "/api/leave", json={"employee_id": before[0], "date": day}
        )
        after = self.client.get(
            f"/api/calendar?start={day}&end={self.day_after(day)}"
        ).get_json()[0]

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(before[0], after["extendedProps"]["employee_ids"])
        self.assertEqual(len(set(after["extendedProps"]["employee_ids"])), 2)
        self.assertIn(
            next(
                employee["name"]
                for employee in self.employees
                if employee["id"] == before[0]
            ),
            after["extendedProps"]["leave_names"],
        )

        renamed_name = "Renamed Employee"
        renamed = self.client.patch(
            f"/api/employees/{before[0]}", json={"name": renamed_name}
        )
        renamed_event = self.client.get(
            f"/api/calendar?start={day}&end={self.day_after(day)}"
        ).get_json()[0]
        export = self.client.get(
            f"/api/export.csv?start={day}&end={self.day_after(day)}"
        ).data.decode("utf-8-sig")

        self.assertEqual(renamed.status_code, 200)
        self.assertIn(renamed_name, renamed_event["extendedProps"]["leave_names"])
        self.assertIn(renamed_name, export)

    def test_canceling_leave_rebalances_saturday_with_returning_employee_eligible(self):
        self.client.delete(f'/api/employees/{self.employees[-1]["id"]}')
        day = self.next_saturday().isoformat()
        day_after = self.day_after(day)
        initial = self.client.get(
            f"/api/calendar?start={day}&end={day_after}"
        ).get_json()[0]
        returning_employee_id = initial["extendedProps"]["employee_ids"][0]

        marked_leave = self.client.post(
            "/api/leave",
            json={"employee_id": returning_employee_id, "date": day},
        )
        after_leave = self.client.get(
            f"/api/calendar?start={day}&end={day_after}"
        ).get_json()[0]
        canceled_leave = self.client.delete(
            "/api/leave",
            json={"employee_id": returning_employee_id, "date": day},
        )
        after_return = self.client.get(
            f"/api/calendar?start={day}&end={day_after}"
        ).get_json()[0]

        self.assertEqual(marked_leave.status_code, 200)
        self.assertNotIn(
            returning_employee_id, after_leave["extendedProps"]["employee_ids"]
        )
        self.assertEqual(canceled_leave.status_code, 200)
        self.assertIn(
            returning_employee_id, after_return["extendedProps"]["employee_ids"]
        )
        self.assertEqual(after_return["extendedProps"]["leave_names"], [])

    def test_pending_leave_request_does_not_change_roster_until_range_is_approved(self):
        start = "2027-10-02"
        end = "2027-10-10"
        employee_id = self.employees[0]["id"]
        before = self.client.get(
            f"/api/calendar?start={start}&end=2027-10-17"
        ).get_json()
        request_response = self.client.post(
            "/api/leave-requests",
            json={
                "employee_id": employee_id,
                "start_date": start,
                "end_date": end,
            },
        )
        after_pending = self.client.get(
            f"/api/calendar?start={start}&end=2027-10-17"
        ).get_json()

        self.assertEqual(request_response.status_code, 201)
        self.assertEqual(
            [
                event["extendedProps"]["employee_ids"]
                for event in before
            ],
            [
                event["extendedProps"]["employee_ids"]
                for event in after_pending
            ],
        )
        self.assertEqual(
            self.client.get("/api/leave?date=2027-10-02").get_json()["employee_ids"],
            [],
        )
        pending = self.client.get(
            "/api/leave-requests?status=pending"
        ).get_json()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["start_date"], start)
        self.assertEqual(pending[0]["end_date"], end)

        approved = self.client.patch(
            f"/api/leave-requests/{request_response.get_json()['id']}",
            json={"status": "approved"},
        )
        approved_saturdays = self.client.get(
            "/api/calendar?start=2027-10-01&end=2027-10-11"
        ).get_json()
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.get_json()["status"], "approved")
        self.assertEqual(len(approved_saturdays), 2)
        for event in approved_saturdays:
            self.assertNotIn(employee_id, event["extendedProps"]["employee_ids"])
            self.assertIn(
                self.employees[0]["name"], event["extendedProps"]["leave_names"]
            )

    def test_rejected_leave_request_does_not_create_leave(self):
        request_response = self.client.post(
            "/api/leave-requests",
            json={
                "employee_id": self.employees[0]["id"],
                "start_date": "2027-10-02",
                "end_date": "2027-10-10",
            },
        )
        rejected = self.client.patch(
            f"/api/leave-requests/{request_response.get_json()['id']}",
            json={"status": "rejected"},
        )

        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(
            self.client.get("/api/leave?date=2027-10-02").get_json()["employee_ids"],
            [],
        )
        self.assertEqual(
            self.client.get("/api/leave-requests?status=rejected").get_json()[0]["status"],
            "rejected",
        )

    def test_pending_leave_request_forecasts_coverage_risks(self):
        day = "2027-10-02"
        for employee in self.employees[1:3]:
            response = self.client.post(
                "/api/leave",
                json={"employee_id": employee["id"], "date": day},
            )
            self.assertEqual(response.status_code, 200)
        request_response = self.client.post(
            "/api/leave-requests",
            json={
                "employee_id": self.employees[0]["id"],
                "start_date": day,
                "end_date": "2027-10-10",
            },
        )

        pending = self.client.get(
            "/api/leave-requests?status=pending"
        ).get_json()[0]

        self.assertEqual(request_response.status_code, 201)
        self.assertEqual(
            pending["coverage_forecast"],
            {
                "open_saturdays_considered": 2,
                "coverage_risks": [
                    {"date": "2027-10-02", "available_after_approval": 1},
                ],
            },
        )

    def test_pending_leave_request_forecast_excludes_closed_saturdays(self):
        employee_id = self.employees[0]["id"]
        self.client.put(
            "/api/holidays/2027-10-02",
            json={"name": "Closed", "dc_closed": True},
        )
        self.client.post(
            "/api/leave-requests",
            json={
                "employee_id": employee_id,
                "start_date": "2027-10-02",
                "end_date": "2027-10-02",
            },
        )

        pending = self.client.get(
            "/api/leave-requests?status=pending"
        ).get_json()[0]

        self.assertEqual(
            pending["coverage_forecast"],
            {"open_saturdays_considered": 0, "coverage_risks": []},
        )

    def test_leave_request_rejects_invalid_dates_and_overlapping_pending_request(self):
        employee_id = self.employees[0]["id"]
        invalid = self.client.post(
            "/api/leave-requests",
            json={
                "employee_id": employee_id,
                "start_date": "2027-10-10",
                "end_date": "2027-10-02",
            },
        )
        first = self.client.post(
            "/api/leave-requests",
            json={
                "employee_id": employee_id,
                "start_date": "2027-10-02",
                "end_date": "2027-10-10",
            },
        )
        overlap = self.client.post(
            "/api/leave-requests",
            json={
                "employee_id": employee_id,
                "start_date": "2027-10-09",
                "end_date": "2027-10-15",
            },
        )

        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(overlap.status_code, 409)

    def test_manual_override_is_saved_and_rejects_employee_on_leave(self):
        day = self.next_saturday().isoformat()
        ids = [employee["id"] for employee in self.employees]
        response = self.client.post(
            f"/api/assignments/{day}", json={"employee_ids": ids[2:4]}
        )
        self.assertEqual(response.status_code, 200)
        event = self.client.get(
            f"/api/calendar?start={day}&end={self.day_after(day)}"
        ).get_json()[0]
        self.assertEqual(event["extendedProps"]["employee_ids"], ids[2:4])
        self.assertTrue(event["extendedProps"]["manual"])

        self.client.post("/api/leave", json={"employee_id": ids[2], "date": day})
        rejected = self.client.post(
            f"/api/assignments/{day}", json={"employee_ids": ids[2:4]}
        )
        self.assertEqual(rejected.status_code, 400)

    def test_substitute_recommendations_rank_available_staff_by_workload(self):
        added = self.client.post("/api/employees", json={"name": "Emery"}).get_json()
        ids = [employee["id"] for employee in self.employees]
        self.client.post(
            "/api/assignments/2027-10-02",
            json={"employee_ids": ids[2:4]},
        )
        self.client.post(
            "/api/assignments/2027-10-09",
            json={"employee_ids": [ids[0], ids[1]]},
        )

        response = self.client.get("/api/substitutes/2027-10-09")

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["assigned_employee_ids"], ids[:2])
        self.assertEqual(
            [employee["id"] for employee in result["substitutes"]],
            [added["id"], ids[2], ids[3]],
        )
        self.assertEqual(
            [employee["assignments_this_month"] for employee in result["substitutes"]],
            [0, 1, 1],
        )
        self.assertEqual(
            [employee["worked_this_month"] for employee in result["substitutes"]],
            [0, 0, 0],
        )
        self.assertTrue(result["substitutes"][1]["worked_previous_saturday"])

    def test_actual_substitute_work_breaks_ties_in_future_automatic_rotation(self):
        added = self.client.post("/api/employees", json={"name": "Emery"}).get_json()
        ids = [employee["id"] for employee in self.employees]
        self.client.post(
            "/api/assignments/2027-10-02",
            json={"employee_ids": ids[:2]},
        )
        self.client.post(
            "/api/assignments/2027-10-09",
            json={"employee_ids": ids[2:4]},
        )
        for day in ("2027-10-02", "2027-10-09"):
            self.client.put(
                f"/api/attendance/{day}",
                json={"employee_ids": [ids[0]]},
            )

        event = self.client.get(
            "/api/calendar?start=2027-10-16&end=2027-10-17"
        ).get_json()[0]

        self.assertEqual(
            event["extendedProps"]["employee_ids"],
            [added["id"], ids[1]],
        )

    def test_automatic_rotation_favors_lower_recent_workload_when_monthly_loads_tie(self):
        ids = [employee["id"] for employee in self.employees]
        with self.app.app_context():
            db = get_db()
            db.executemany(
                """
                INSERT INTO assignments (date, employee1_id, employee2_id, manual)
                VALUES (?, ?, ?, 1)
                """,
                [
                    (day, ids[0], ids[1])
                    for day in ("2027-09-04", "2027-09-11", "2027-09-18")
                ],
            )
            db.commit()

        event = self.client.get(
            "/api/calendar?start=2027-10-02&end=2027-10-03"
        ).get_json()[0]

        self.assertEqual(len(event["extendedProps"]["employee_ids"]), 2)
        self.assertIn(ids[2], event["extendedProps"]["employee_ids"])

    def test_substitute_recommendations_exclude_leave_and_recurring_unavailability(self):
        added = self.client.post("/api/employees", json={"name": "Emery"}).get_json()
        ids = [employee["id"] for employee in self.employees]
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO leave_days (employee_id, date) VALUES (?, ?)",
                (ids[2], "2027-10-09"),
            )
            db.execute(
                "INSERT INTO employee_unavailable_weekdays (employee_id, weekday) VALUES (?, ?)",
                (ids[3], 5),
            )
            db.commit()
        self.client.post(
            "/api/assignments/2027-10-09",
            json={"employee_ids": ids[:2]},
        )

        result = self.client.get("/api/substitutes/2027-10-09").get_json()

        self.assertEqual(
            [employee["id"] for employee in result["substitutes"]],
            [added["id"]],
        )

    def test_manual_assignment_rejects_recurring_unavailability(self):
        employee_id = self.employees[0]["id"]
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO employee_unavailable_weekdays (employee_id, weekday) VALUES (?, ?)",
                (employee_id, 5),
            )
            db.commit()

        response = self.client.post(
            "/api/assignments/2027-10-09",
            json={
                "employee_ids": [
                    employee_id,
                    self.employees[1]["id"],
                ]
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("unavailable", response.get_json()["error"].lower())

    def test_clearing_future_manual_assignment_restores_automatic_rotation(self):
        day = "2027-10-09"
        ids = [employee["id"] for employee in self.employees]
        self.client.get("/api/calendar?start=2027-10-01&end=2027-10-16")
        overridden = self.client.post(
            f"/api/assignments/{day}",
            json={"employee_ids": ids[2:4]},
        )

        cleared = self.client.delete(f"/api/assignments/{day}")
        event = self.client.get(
            f"/api/calendar?start={day}&end=2027-10-10"
        ).get_json()[0]

        self.assertEqual(overridden.status_code, 200)
        self.assertEqual(cleared.status_code, 200)
        self.assertFalse(event["extendedProps"]["manual"])
        self.assertEqual(len(event["extendedProps"]["employee_ids"]), 2)
        self.assertEqual(cleared.get_json()["employee_names"], event["extendedProps"]["employee_names"])
        history = self.client.get("/api/activity").get_json()["entries"]
        self.assertEqual(history[0]["action"], "Saturday override cleared")

    def test_clearing_assignment_without_manual_override_returns_conflict(self):
        response = self.client.delete("/api/assignments/2027-10-09")
        self.assertEqual(response.status_code, 409)

    def test_clearing_past_manual_assignment_is_rejected_and_kept(self):
        past_day = "2020-10-03"
        ids = [employee["id"] for employee in self.employees]
        with self.app.app_context():
            db = get_db()
            db.execute(
                """
                INSERT INTO assignments (date, employee1_id, employee2_id, manual)
                VALUES (?, ?, ?, 1)
                """,
                (past_day, ids[0], ids[1]),
            )
            db.commit()

        response = self.client.delete(f"/api/assignments/{past_day}")

        self.assertEqual(response.status_code, 409)
        with self.app.app_context():
            assignment = get_db().execute(
                "SELECT manual FROM assignments WHERE date = ?",
                (past_day,),
            ).fetchone()
        self.assertIsNotNone(assignment)
        self.assertTrue(assignment["manual"])

    def test_clearing_manual_assignment_keeps_it_if_coverage_would_fail(self):
        day = "2027-10-09"
        ids = [employee["id"] for employee in self.employees]
        with self.app.app_context():
            db = get_db()
            db.execute(
                """
                INSERT INTO assignments (date, employee1_id, employee2_id, manual)
                VALUES (?, ?, ?, 1)
                """,
                (day, ids[2], ids[3]),
            )
            db.executemany(
                "INSERT INTO leave_days (employee_id, date) VALUES (?, ?)",
                [(ids[0], day), (ids[1], day), (ids[2], day)],
            )
            db.commit()

        response = self.client.delete(f"/api/assignments/{day}")

        self.assertEqual(response.status_code, 409)
        with self.app.app_context():
            assignment = get_db().execute(
                "SELECT employee1_id, employee2_id, manual FROM assignments WHERE date = ?",
                (day,),
            ).fetchone()
        self.assertEqual(assignment["employee1_id"], ids[2])
        self.assertEqual(assignment["employee2_id"], ids[3])
        self.assertTrue(assignment["manual"])

    def test_substitute_endpoint_rejects_non_saturday(self):
        response = self.client.get("/api/substitutes/2027-10-08")
        self.assertEqual(response.status_code, 400)

    def test_pair_exclusion_is_respected_by_automatic_scheduler(self):
        ids = [employee["id"] for employee in self.employees]
        restriction = self.client.post(
            "/api/pair-exclusions",
            json={"employee_ids": ids[:2]},
        )

        events = self.client.get(
            "/api/calendar?start=2027-10-01&end=2027-10-16"
        ).get_json()

        self.assertEqual(restriction.status_code, 201)
        self.assertEqual(len(events), 2)
        for event in events:
            self.assertNotEqual(
                set(event["extendedProps"]["employee_ids"]),
                set(ids[:2]),
            )

    def test_pair_exclusion_rejects_same_or_inactive_employee_ids(self):
        same_employee = self.client.post(
            "/api/pair-exclusions",
            json={
                "employee_ids": [
                    self.employees[0]["id"],
                    self.employees[0]["id"],
                ]
            },
        )
        self.client.delete(f'/api/employees/{self.employees[3]["id"]}')
        inactive_employee = self.client.post(
            "/api/pair-exclusions",
            json={
                "employee_ids": [
                    self.employees[0]["id"],
                    self.employees[3]["id"],
                ]
            },
        )

        self.assertEqual(same_employee.status_code, 400)
        self.assertEqual(inactive_employee.status_code, 400)

    def test_manual_assignment_rejects_restricted_pair(self):
        employee_ids = [employee["id"] for employee in self.employees[:2]]
        self.client.post(
            "/api/pair-exclusions",
            json={"employee_ids": employee_ids},
        )

        response = self.client.post(
            "/api/assignments/2027-10-09",
            json={"employee_ids": employee_ids},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("restricted", response.get_json()["error"].lower())

    def test_pair_exclusion_cannot_conflict_with_future_manual_assignment(self):
        employee_ids = [employee["id"] for employee in self.employees[:2]]
        self.client.post(
            "/api/assignments/2027-10-09",
            json={"employee_ids": employee_ids},
        )

        response = self.client.post(
            "/api/pair-exclusions",
            json={"employee_ids": employee_ids},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.get("/api/pair-exclusions").get_json(), [])

    def test_pair_restrictions_report_gap_when_no_feasible_pair_remains(self):
        ids = [employee["id"] for employee in self.employees]
        for index, first_id in enumerate(ids):
            for second_id in ids[index + 1 :]:
                response = self.client.post(
                    "/api/pair-exclusions",
                    json={"employee_ids": [first_id, second_id]},
                )
                self.assertIn(response.status_code, (200, 201))

        summary = self.client.get(
            "/api/summary?start=2027-10-02&end=2027-10-03"
        ).get_json()
        event = self.client.get(
            "/api/calendar?start=2027-10-02&end=2027-10-03"
        ).get_json()[0]

        self.assertEqual(summary["coverage_gaps"], 1)
        self.assertTrue(event["extendedProps"]["coverage_issue"])

    def test_pair_restriction_can_be_removed_and_scheduler_rebalances(self):
        ids = [employee["id"] for employee in self.employees[:2]]
        added = self.client.post(
            "/api/pair-exclusions",
            json={"employee_ids": ids},
        )
        removed = self.client.delete(f"/api/pair-exclusions/{ids[1]}/{ids[0]}")

        self.assertEqual(added.status_code, 201)
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(self.client.get("/api/pair-exclusions").get_json(), [])

    def test_csv_export_contains_visible_roster(self):
        response = self.client.get(
            "/api/export.csv?start=2026-10-01&end=2026-11-01"
        )
        rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            rows[0],
            [
                "Date",
                "Employee 1",
                "Employee 2",
                "Actually worked",
                "Attendance recorded",
                "On leave",
                "Manual override",
                "Coverage",
            ],
        )
        self.assertEqual(len(rows), 6)

    def test_backup_can_replace_database_and_keeps_safety_copy(self):
        source_database = str(Path(self.temp_dir.name) / "source.db")
        source_client = create_app(
            {"TESTING": True, "DATABASE": source_database}
        ).test_client()
        self.sign_up_admin(source_client)
        for name in ("Restored One", "Restored Two", "Restored Three"):
            self.assertEqual(
                source_client.post("/api/employees", json={"name": name}).status_code,
                201,
            )
        source_db = sqlite3.connect(source_database)
        try:
            source_db.execute("DROP TABLE activity_log")
            source_db.commit()
        finally:
            source_db.close()
        backup = source_client.get("/api/backup")
        backup_bytes = backup.get_data()
        backup.close()
        current_database_path = Path(self.app.config["DATABASE"])
        before_names = {
            employee["name"] for employee in self.client.get("/api/employees").get_json()
        }

        restored = self.client.post(
            "/api/restore",
            data={
                "confirm_replace": "true",
                "backup": (io.BytesIO(backup_bytes), "roster-backup.db"),
            },
            content_type="multipart/form-data",
        )
        after_names = {
            employee["name"] for employee in self.client.get("/api/employees").get_json()
        }
        restored_history = self.client.get("/api/activity").get_json()["entries"]
        safety_copies = list(
            current_database_path.parent.glob(
                f"{current_database_path.stem}-pre-restore-*.db"
            )
        )

        self.assertEqual(backup.status_code, 200)
        self.assertIn("attachment", backup.headers["Content-Disposition"])
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(before_names, {"Avery", "Bailey", "Casey", "Devon"})
        self.assertEqual(after_names, {"Restored One", "Restored Two", "Restored Three"})
        self.assertEqual(restored_history[0]["action"], "Database restored")
        self.assertIn("pre-restore", restored_history[0]["details"])
        self.assertEqual(len(safety_copies), 1)
        safety_client = create_app(
            {"TESTING": True, "DATABASE": str(safety_copies[0])}
        ).test_client()
        self.sign_in(safety_client)
        self.assertEqual(
            {employee["name"] for employee in safety_client.get("/api/employees").get_json()},
            before_names,
        )

    def test_operator_cannot_manage_user_accounts_or_backups(self):
        created = self.client.post(
            "/api/users",
            json={
                "username": "operator",
                "password": "operator-test-password",
                "role": "operator",
            },
        )
        operator = self.app.test_client()
        signed_in = self.sign_in(
            operator,
            username="operator",
            password="operator-test-password",
        )

        users_response = operator.get("/api/users")
        backup_response = operator.get("/api/backup")
        add_user_response = operator.post(
            "/api/users",
            json={
                "username": "unauthorized",
                "password": "unauthorized-password",
                "role": "viewer",
            },
        )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(signed_in.status_code, 302)
        self.assertEqual(users_response.status_code, 403)
        self.assertEqual(backup_response.status_code, 403)
        self.assertEqual(add_user_response.status_code, 403)

    def test_last_active_admin_cannot_be_demoted_or_deactivated(self):
        user_id = self.client.get("/api/users").get_json()[0]["id"]
        demote = self.client.patch(
            f"/api/users/{user_id}",
            json={"role": "operator"},
        )
        deactivate = self.client.patch(
            f"/api/users/{user_id}",
            json={"active": False},
        )

        self.assertEqual(demote.status_code, 409)
        self.assertEqual(deactivate.status_code, 409)

    def test_admin_cannot_demote_their_own_account_when_another_admin_exists(self):
        added_admin = self.client.post(
            "/api/users",
            json={
                "username": "second-admin",
                "password": "second-admin-test-password",
                "role": "admin",
            },
        )
        current_admin_id = self.client.get("/api/users").get_json()[0]["id"]

        response = self.client.patch(
            f"/api/users/{current_admin_id}",
            json={"role": "operator"},
        )

        self.assertEqual(added_admin.status_code, 201)
        self.assertEqual(response.status_code, 409)

    def test_restore_requires_confirmation_and_rejects_invalid_database(self):
        original_names = {
            employee["name"] for employee in self.client.get("/api/employees").get_json()
        }
        unconfirmed = self.client.post(
            "/api/restore",
            data={"backup": (io.BytesIO(b"not sqlite"), "bad.db")},
            content_type="multipart/form-data",
        )
        invalid = self.client.post(
            "/api/restore",
            data={
                "confirm_replace": "true",
                "backup": (io.BytesIO(b"not sqlite"), "bad.db"),
            },
            content_type="multipart/form-data",
        )
        after_names = {
            employee["name"] for employee in self.client.get("/api/employees").get_json()
        }

        self.assertEqual(unconfirmed.status_code, 400)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(after_names, original_names)

    def test_actual_attendance_records_substitute_and_cancels_leave_for_attendee(self):
        day = self.next_saturday().isoformat()
        day_after = self.day_after(day)
        employee_ids = [employee["id"] for employee in self.employees]
        on_leave_id, substitute_id, *_ = employee_ids
        self.client.post(
            "/api/leave", json={"employee_id": on_leave_id, "date": day}
        )

        saved = self.client.put(
            f"/api/attendance/{day}",
            json={"employee_ids": [on_leave_id, substitute_id]},
        )
        attendance = self.client.get(f"/api/attendance?date={day}").get_json()
        leave = self.client.get(f"/api/leave?date={day}").get_json()
        events = self.client.get(
            f"/api/calendar?start={day}&end={day_after}"
        ).get_json()
        event = next(event for event in events if not event["extendedProps"].get("holiday"))
        export = list(
            csv.reader(
                io.StringIO(
                    self.client.get(
                        f"/api/export.csv?start={day}&end={day_after}"
                    ).data.decode("utf-8-sig")
                )
            )
        )

        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.get_json()["leave_canceled"], [on_leave_id])
        self.assertTrue(attendance["recorded"])
        self.assertEqual(set(attendance["employee_ids"]), {on_leave_id, substitute_id})
        self.assertEqual(leave["employee_ids"], [])
        self.assertEqual(
            set(event["extendedProps"]["actual_employee_ids"]),
            {on_leave_id, substitute_id},
        )
        expected_names = {
            employee["name"]
            for employee in self.employees
            if employee["id"] in {on_leave_id, substitute_id}
        }
        self.assertEqual(set(event["extendedProps"]["actual_names"]), expected_names)
        self.assertEqual(export[1][3], "; ".join(event["extendedProps"]["actual_names"]))
        self.assertEqual(export[1][4], "Yes")

    def test_workload_summary_distinguishes_scheduled_from_actual_worked(self):
        day = "2027-10-02"
        scheduled = self.client.get(
            f"/api/calendar?start={day}&end={self.day_after(day)}"
        ).get_json()[0]["extendedProps"]["employee_ids"]
        worked = [
            employee["id"]
            for employee in self.employees
            if employee["id"] not in scheduled
        ][:2]
        self.assertEqual(len(worked), 2)
        self.client.put(
            f"/api/attendance/{day}", json={"employee_ids": worked}
        )

        summary = self.client.get(
            "/api/summary?start=2027-10-01&end=2027-10-08"
        ).get_json()
        rows = {row["id"]: row for row in summary["employees"]}

        for employee_id in scheduled:
            self.assertEqual(rows[employee_id]["saturday_count"], 1)
            self.assertEqual(rows[employee_id]["worked_count"], 0)
        for employee_id in worked:
            self.assertEqual(rows[employee_id]["saturday_count"], 0)
            self.assertEqual(rows[employee_id]["worked_count"], 1)

    def test_workload_summary_warns_when_actual_work_is_unbalanced(self):
        days = ("2027-10-02", "2027-10-09")
        attendance = (
            [self.employees[0]["id"], self.employees[1]["id"]],
            [self.employees[0]["id"]],
        )
        for day, employee_ids in zip(days, attendance):
            response = self.client.put(
                f"/api/attendance/{day}",
                json={"employee_ids": employee_ids},
            )
            self.assertEqual(response.status_code, 200)

        summary = self.client.get(
            "/api/summary?start=2027-10-01&end=2027-10-16"
        ).get_json()

        self.assertEqual(summary["actual_work_spread"], 2)
        self.assertIn(
            "actual saturdays worked",
            summary["actual_work_warning"].lower(),
        )

    def test_workload_summary_does_not_warn_when_actual_work_is_balanced(self):
        day = "2027-10-02"
        self.client.put(
            f"/api/attendance/{day}",
            json={
                "employee_ids": [
                    self.employees[0]["id"],
                    self.employees[1]["id"],
                ]
            },
        )

        summary = self.client.get(
            "/api/summary?start=2027-10-01&end=2027-10-08"
        ).get_json()

        self.assertEqual(summary["actual_work_spread"], 1)
        self.assertIsNone(summary["actual_work_warning"])

    def test_attendance_requires_saturday_and_distinct_existing_employee_ids(self):
        monday = "2027-09-06"
        saturday = "2027-09-04"
        wrong_day = self.client.put(
            f"/api/attendance/{monday}", json={"employee_ids": []}
        )
        duplicate = self.client.put(
            f"/api/attendance/{saturday}",
            json={"employee_ids": [self.employees[0]["id"]] * 2},
        )
        missing = self.client.put(
            f"/api/attendance/{saturday}", json={"employee_ids": [9999]}
        )

        self.assertEqual(wrong_day.status_code, 400)
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(missing.status_code, 400)

    def test_workload_summary_includes_zero_assignment_employees(self):
        self.client.get("/api/calendar?start=2026-10-03&end=2026-10-04")
        summary = self.client.get(
            "/api/summary?start=2026-10-03&end=2026-10-04"
        ).get_json()

        self.assertEqual(len(summary["employees"]), 4)
        self.assertEqual(sum(row["saturday_count"] for row in summary["employees"]), 2)

    def test_leave_that_leaves_fewer_than_two_employees_shows_coverage_gap(self):
        ids = [employee["id"] for employee in self.employees]
        day = self.next_saturday().isoformat()
        for employee_id in ids[:3]:
            self.client.post(
                "/api/leave", json={"employee_id": employee_id, "date": day}
            )

        events = self.client.get(
            f"/api/calendar?start={day}&end={self.day_after(day)}"
        ).get_json()
        summary = self.client.get(
            f"/api/summary?start={day}&end={self.day_after(day)}"
        ).get_json()

        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["extendedProps"]["coverage_issue"])
        self.assertEqual(summary["coverage_gaps"], 1)
        self.assertIsNotNone(summary["warning"])

    def test_removing_leave_restores_automatic_assignment(self):
        ids = [employee["id"] for employee in self.employees]
        day = self.next_saturday().isoformat()
        self.client.post("/api/leave", json={"employee_id": ids[0], "date": day})
        self.client.post("/api/leave", json={"employee_id": ids[1], "date": day})
        restored = self.client.delete(
            "/api/leave", json={"employee_id": ids[1], "date": day}
        )

        events = self.client.get(
            f"/api/calendar?start={day}&end={self.day_after(day)}"
        ).get_json()
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0]["extendedProps"]["employee_ids"]), 2)

    def test_employee_removal_rebalances_future_assignments_and_preserves_minimum(self):
        ids = [employee["id"] for employee in self.employees]
        start = "2027-09-01"
        end = "2027-10-01"
        self.client.get(f"/api/calendar?start={start}&end={end}")
        manual_day = "2027-09-04"
        self.client.post(
            f"/api/assignments/{manual_day}",
            json={"employee_ids": [ids[3], ids[0]]},
        )

        removed = self.client.delete(f"/api/employees/{ids[3]}")
        blocked = self.client.delete(f"/api/employees/{ids[2]}")
        events = self.client.get(f"/api/calendar?start={start}&end={end}").get_json()
        active = self.client.get("/api/employees").get_json()
        active_ids = {employee["id"] for employee in active}

        self.assertEqual(removed.status_code, 200)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(len(active), 3)
        self.assertEqual(len(events), 4)
        for event in events:
            self.assertEqual(len(event["extendedProps"]["employee_ids"]), 2)
            self.assertTrue(set(event["extendedProps"]["employee_ids"]) <= active_ids)
        invalidated_manual = next(event for event in events if event["start"] == manual_day)
        self.assertNotIn(ids[3], invalidated_manual["extendedProps"]["employee_ids"])
        self.assertFalse(invalidated_manual["extendedProps"]["manual"])

    def test_employee_addition_rebalances_existing_future_auto_assignments(self):
        start = "2027-10-01"
        end = "2027-11-01"
        self.client.get(f"/api/calendar?start={start}&end={end}")
        manual_day = "2027-10-02"
        manual_ids = [self.employees[0]["id"], self.employees[1]["id"]]
        self.client.post(
            f"/api/assignments/{manual_day}",
            json={"employee_ids": manual_ids},
        )

        added = self.client.post("/api/employees", json={"name": "Emery"})
        events = self.client.get(f"/api/calendar?start={start}&end={end}").get_json()
        summary = self.client.get(
            f"/api/summary?start={start}&end={end}"
        ).get_json()
        assignments_per_employee = [
            employee["saturday_count"] for employee in summary["employees"]
        ]

        self.assertEqual(added.status_code, 201)
        self.assertEqual(len(events), 5)
        self.assertEqual(len(assignments_per_employee), 5)
        self.assertEqual(assignments_per_employee, [2, 2, 2, 2, 2])
        preserved_manual = next(event for event in events if event["start"] == manual_day)
        self.assertEqual(preserved_manual["extendedProps"]["employee_ids"], manual_ids)
        self.assertTrue(preserved_manual["extendedProps"]["manual"])

    def test_three_employee_workload_stays_balanced_in_five_saturday_month(self):
        ids = [employee["id"] for employee in self.employees]
        self.client.delete(f"/api/employees/{ids[3]}")

        summary = self.client.get(
            "/api/summary?start=2027-10-01&end=2027-11-01"
        ).get_json()
        counts = [employee["saturday_count"] for employee in summary["employees"]]

        self.assertEqual(sorted(counts), [3, 3, 4])
        self.assertIn("2 Saturdays off", summary["warning"])
        self.assertTrue(summary["saturday_off_shortfalls"])

    def test_four_employees_meet_two_saturdays_off_minimum_in_five_saturday_month(self):
        summary = self.client.get(
            "/api/summary?start=2027-10-01&end=2027-11-01"
        ).get_json()

        self.assertEqual(summary["minimum_saturdays_off"], 2)
        self.assertTrue(
            all(employee["saturdays_off_count"] >= 2 for employee in summary["employees"])
        )
        self.assertIsNone(summary["warning"])

    def test_workload_reports_saturdays_off_in_visible_period(self):
        summary = self.client.get(
            "/api/summary?start=2027-10-01&end=2027-10-08"
        ).get_json()

        self.assertEqual(
            sum(employee["saturdays_off_count"] for employee in summary["employees"]),
            2,
        )

    def test_inactive_employee_can_be_reactivated_without_losing_identity(self):
        employee = self.employees[-1]
        self.client.delete(f'/api/employees/{employee["id"]}')

        response = self.client.post(
            "/api/employees", json={"name": employee["name"]}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["id"], employee["id"])

    def test_rename_updates_calendar_and_export_without_changing_assignment_identity(self):
        employee_id = self.employees[0]["id"]
        day = self.next_saturday().isoformat()
        day_after = self.day_after(day)
        before = self.client.get(
            f"/api/calendar?start={day}&end={day_after}"
        ).get_json()[0]

        renamed = self.client.patch(
            f"/api/employees/{employee_id}", json={"name": "Alex Morgan"}
        )
        after = self.client.get(
            f"/api/calendar?start={day}&end={day_after}"
        ).get_json()[0]
        export = self.client.get(
            f"/api/export.csv?start={day}&end={day_after}"
        ).data.decode("utf-8-sig")

        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(after["extendedProps"]["employee_ids"], before["extendedProps"]["employee_ids"])
        self.assertIn("Alex Morgan", after["extendedProps"]["employee_names"])
        self.assertIn("Alex Morgan", export)

    def test_rename_rejects_blank_duplicate_and_missing_employee(self):
        employee_id = self.employees[0]["id"]
        blank = self.client.patch(
            f"/api/employees/{employee_id}", json={"name": "   "}
        )
        duplicate = self.client.patch(
            f"/api/employees/{employee_id}", json={"name": self.employees[1]["name"]}
        )
        missing = self.client.patch(
            "/api/employees/9999", json={"name": "New name"}
        )

        self.assertEqual(blank.status_code, 400)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(missing.status_code, 404)
        current = self.client.get("/api/employees").get_json()
        self.assertEqual(current[0]["name"], self.employees[0]["name"])

    def test_manual_assignment_is_replaced_when_leave_makes_it_invalid(self):
        ids = [employee["id"] for employee in self.employees]
        day = self.next_saturday().isoformat()
        self.client.post(
            f"/api/assignments/{day}", json={"employee_ids": ids[:2]}
        )

        self.client.post("/api/leave", json={"employee_id": ids[0], "date": day})
        event = self.client.get(
            f"/api/calendar?start={day}&end={self.day_after(day)}"
        ).get_json()[0]

        self.assertNotIn(ids[0], event["extendedProps"]["employee_ids"])
        self.assertFalse(event["extendedProps"]["manual"])

    def test_recurring_saturday_unavailability_reassigns_and_persists(self):
        ids = [employee["id"] for employee in self.employees]
        day = self.next_saturday().isoformat()
        event = self.client.get(
            f"/api/calendar?start={day}&end={self.day_after(day)}"
        ).get_json()[0]
        employee_id = event["extendedProps"]["employee_ids"][0]

        saved = self.client.put(
            f"/api/employees/{employee_id}/availability",
            json={"weekdays": [5]},
        )
        reassigned = self.client.get(
            f"/api/calendar?start={day}&end={self.day_after(day)}"
        ).get_json()[0]
        availability = self.client.get(
            f"/api/employees/{employee_id}/availability"
        ).get_json()

        self.assertEqual(saved.status_code, 200)
        self.assertNotIn(employee_id, reassigned["extendedProps"]["employee_ids"])
        self.assertEqual(availability["weekdays"], [5])
        self.assertEqual(len(set(reassigned["extendedProps"]["employee_ids"])), 2)
        self.assertTrue(set(reassigned["extendedProps"]["employee_ids"]) <= set(ids))

    def test_recurring_unavailability_reports_short_staffed_saturday(self):
        ids = [employee["id"] for employee in self.employees]
        day = self.next_saturday().isoformat()
        for employee_id in ids[:3]:
            response = self.client.put(
                f"/api/employees/{employee_id}/availability",
                json={"weekdays": [5]},
            )
            self.assertEqual(response.status_code, 200)

        events = self.client.get(
            f"/api/calendar?start={day}&end={self.day_after(day)}"
        ).get_json()
        summary = self.client.get(
            f"/api/summary?start={day}&end={self.day_after(day)}"
        ).get_json()

        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["extendedProps"]["coverage_issue"])
        self.assertEqual(summary["coverage_gaps"], 1)

    def test_recurring_availability_rejects_invalid_weekdays(self):
        employee_id = self.employees[0]["id"]

        invalid = self.client.put(
            f"/api/employees/{employee_id}/availability",
            json={"weekdays": [5, 5]},
        )

        self.assertEqual(invalid.status_code, 400)

    def test_closed_holiday_skips_shift_and_open_holiday_schedules_normally(self):
        day = self.next_saturday().isoformat()
        day_after = self.day_after(day)
        closed = self.client.put(
            f"/api/holidays/{day}",
            json={"name": "Founders Day", "dc_closed": True},
        )
        closed_events = self.client.get(
            f"/api/calendar?start={day}&end={day_after}"
        ).get_json()
        closed_summary = self.client.get(
            f"/api/summary?start={day}&end={day_after}"
        ).get_json()
        export = self.client.get(
            f"/api/export.csv?start={day}&end={day_after}"
        ).data.decode("utf-8-sig")

        self.assertEqual(closed.status_code, 200)
        self.assertEqual(len(closed_events), 1)
        self.assertTrue(closed_events[0]["extendedProps"]["holiday"])
        self.assertTrue(closed_events[0]["extendedProps"]["dc_closed"])
        self.assertEqual(closed_summary["coverage_gaps"], 0)
        self.assertIn("DC closed: Founders Day", export)

        opened = self.client.put(
            f"/api/holidays/{day}",
            json={"name": "Founders Day", "dc_closed": False},
        )
        open_events = self.client.get(
            f"/api/calendar?start={day}&end={day_after}"
        ).get_json()
        assignments = [
            event for event in open_events
            if not event["extendedProps"].get("holiday")
        ]
        holiday_event = next(
            event for event in open_events if event["extendedProps"].get("holiday")
        )
        self.assertEqual(opened.status_code, 200)
        self.assertEqual(len(assignments[0]["extendedProps"]["employee_ids"]), 2)
        self.assertFalse(holiday_event["extendedProps"]["dc_closed"])

    def test_removing_closed_holiday_restores_saturday_assignment(self):
        day = self.next_saturday().isoformat()
        day_after = self.day_after(day)
        self.client.put(
            f"/api/holidays/{day}",
            json={"name": "Holiday", "dc_closed": True},
        )

        removed = self.client.delete(f"/api/holidays/{day}")
        events = self.client.get(
            f"/api/calendar?start={day}&end={day_after}"
        ).get_json()

        self.assertEqual(removed.status_code, 200)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0]["extendedProps"]["employee_ids"]), 2)

    def test_removing_employee_keeps_historical_manual_assignment(self):
        employee = self.employees[-1]
        ids = [item["id"] for item in self.employees]
        days_since_saturday = (date.today().weekday() - 5) % 7 or 7
        past_saturday = date.today() - timedelta(days=days_since_saturday)
        day = past_saturday.isoformat()
        self.client.post(
            f"/api/assignments/{day}",
            json={"employee_ids": [employee["id"], ids[0]]},
        )

        self.client.delete(f'/api/employees/{employee["id"]}')
        event = self.client.get(
            f"/api/calendar?start={day}&end={(past_saturday + timedelta(days=1)).isoformat()}"
        ).get_json()[0]

        self.assertEqual(event["extendedProps"]["employee_ids"], [employee["id"], ids[0]])
        self.assertTrue(event["extendedProps"]["manual"])
        self.assertIn(employee["name"], event["extendedProps"]["employee_names"])

    def test_roster_below_minimum_does_not_show_or_export_assignments(self):
        database = str(Path(self.temp_dir.name) / "undersized.db")
        client = create_app({"TESTING": True, "DATABASE": database}).test_client()
        self.sign_up_admin(client)
        for name in ("Avery", "Bailey"):
            client.post("/api/employees", json={"name": name})

        events = client.get(
            "/api/calendar?start=2027-09-01&end=2027-10-01"
        ).get_json()
        summary = client.get(
            "/api/summary?start=2027-09-01&end=2027-10-01"
        ).get_json()
        csv_rows = list(
            csv.reader(
                io.StringIO(
                    client.get(
                        "/api/export.csv?start=2027-09-01&end=2027-10-01"
                    ).data.decode("utf-8-sig")
                )
            )
        )

        self.assertEqual(events, [])
        self.assertEqual(summary["active_employee_count"], 2)
        self.assertEqual(len(summary["employees"]), 2)
        self.assertEqual(summary["employees"][0]["saturday_count"], 0)
        self.assertIn("at least 3 active employees", summary["warning"].lower())
        self.assertEqual(len(csv_rows), 5)
        self.assertEqual(csv_rows[1][1:3], ["", ""])
        self.assertIn("minimum", csv_rows[1][7])

    def test_invalid_range_is_rejected(self):
        response = self.client.get(
            "/api/calendar?start=2026-10-02&end=2026-10-02"
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
