# Saturday DC Roster

A local Flask and SQLite starter for managing Saturday DC staffing. The active
roster must have at least three employees; two employees are assigned each
Saturday. Automatic assignments are balanced as evenly as possible within each
month, adapting when staff are added or removed.

## Run locally

1. Install Python 3.10 or newer.
2. From this folder, create and activate a virtual environment if desired.
3. Install dependencies: `pip install -r requirements.txt`
4. Start the app: `python app.py`
5. Open <http://127.0.0.1:5000>, create the first administrator account, then
   add at least three employees.

The SQLite database is created at `instance/roster.db` on first run. Set
`ROSTER_DATABASE` to use a different database path, or `PORT` to change the
local server port. A random session-signing key is stored in
`instance/.session-secret` for local use. Keep this file private and preserve it
between restarts so active sessions remain valid.

## Accounts and access

- The first-run setup page creates the initial administrator. Passwords must be
  at least 12 characters and are stored as password hashes.
- Administrators can create accounts and assign the `admin`, `operator`, or
  `viewer` role in the User accounts panel. Operators can manage roster data;
  viewers can read data but cannot change it. Administrators manage accounts
  and backups.
- Mutating requests require a session-bound CSRF token. Activity entries record
  the signed-in username instead of the generic local operator label.
- For multi-device/network deployment, set a long, random `ROSTER_SECRET_KEY`
  consistently on every app worker and set `ROSTER_COOKIE_SECURE=1` behind
  HTTPS. Do not expose Flask's development server directly to the public
  internet; deploy behind a production WSGI server and HTTPS reverse proxy.

## Backup and restore

- Use **Download backup** in the Data Safety panel to download a consistent
  SQLite copy of all employees, assignments, leave, attendance, holidays, and
  requests.
- To restore, select an app backup, check the replacement confirmation, then
  confirm the browser prompt. Restore replaces the current database only after
  validating the SQLite file and required roster tables.
- Before replacement, the app creates a safety backup beside the configured
  database file. Keep that file until you have verified the restored roster.
- Backups are limited to 64 MB on upload. Restore accepts backups made by this
  app with a compatible schema; it rejects invalid or incomplete database files.

## Use the roster

- Add at least three employees in the Employees panel before scheduling. The
  app will not let the active roster drop below three.
- Use **Import employees from CSV** to add or reactivate up to 500 employees in
  one upload. Use the downloadable one-column `Name` template. Existing active
  names and repeated names in the import are skipped; inactive matches are
  reactivated. The import is validated before applying any changes, and a
  summary is shown after upload.
- Use **Export roster CSV** to download the current employee roster snapshot with
  each employee ID, current name, and active/inactive status for admin records.
- Add a **Team pairing restriction** when two active employees must not be
  scheduled together. The automatic scheduler searches the remaining available
  pairs while keeping workload fairness as its primary objective. Manual
  assignments and substitute selections also respect restrictions. A new rule
  is rejected if it conflicts with a future manual assignment; if restrictions
  leave no valid two-person team for a Saturday, that date is reported as a
  coverage gap instead of violating a constraint.
- Remove employees to deactivate them without deleting their past roster
  history. Upcoming automatic assignments are rebalanced after each staff
  change. Re-adding an inactive employee by the same name restores that record.
- Rename an employee without changing their employee ID; existing assignments,
  leave records, calendar entries, and exports keep the same employee identity
  and display the updated name.
- Select a Saturday to choose its two assigned employees or mark/unmark leave.
  Calendar entries identify both the assigned employees and anyone on leave;
  CSV exports include an `On leave` column as well.
  The Saturday planner also ranks available substitute options by their
  scheduled workload and actual attendance this month, then whether they
  worked the previous Saturday. In automatic rotation, scheduled assignment
  counts remain the primary fairness measure; actual attendance breaks ties so
  substitute work is also considered.
  Choose which assigned employee to replace; the suggestion only updates the
  planner selection, and the override takes effect only after you save it.
  For today or a future Saturday, use **Return to automatic rotation** to clear
  a manual override and let the fairness scheduler choose the team again. The
  app keeps the override if fewer than two employees are available, and past
  assignments cannot be rewritten this way.
  If an employee on leave comes in and works, unmark their leave in the
  Saturday planner. The leave is canceled and the Saturday is rebalanced with
  that employee eligible under the normal rotation rules.
- Submit leave for a date range in the Leave requests panel. Requests remain
  pending and do not affect the roster until approved. Approving adds leave for
  every date in the range and rebalances upcoming Saturdays; rejecting a
  request does not change assignments. Overlapping pending or approved leave
  requests for the same employee are prevented. Each pending request includes
  an individual forecast of future open Saturdays that would have fewer than
  two available employees if approved. Forecasts are evaluated one request at
  a time and do not combine other pending requests.
- Use **Import approved leave from CSV** to add up to 500 employee/date rows
  immediately. Download the `Employee,Date` template and use `YYYY-MM-DD`
  dates. Active employee names are matched without case sensitivity; repeated
  rows and leave already recorded are skipped. Invalid employees, dates, or
  rows overlapping a pending request reject the whole import without partial
  changes. New leave is logged and upcoming assignments are rebalanced.
- Use **What-if leave preview** to see the proposed team and monthly scheduled
  workload if one employee were unavailable on a Saturday. The preview applies
  the normal monthly rotation in an isolated database copy; it does not save
  leave, assignments, or activity history.
- Use **Preview employee change** to simulate adding or removing one employee
  from a selected Saturday onward. It compares the remaining Saturday teams and
  scheduled workload over the next 90 days, and flags coverage gaps if the
  hypothetical roster falls below the three-employee minimum or a Saturday has
  fewer than two available employees. The simulation changes only an in-memory
  copy; add/remove the employee separately to apply the change.
- After a Saturday, use **Actual attendance** in its planner to record everyone
  who really worked, including substitutes not on the planned assignment.
  Actual attendance is kept separately from the planned assignment and appears
  in the calendar and CSV export. Recording someone who was marked on leave
  clears their leave record and rebalances upcoming shifts.
- Select an employee's recurring unavailable weekdays in the date planner. The
  Saturday rule is applied to all future automatic Saturday assignments.
- Mark any calendar date as a holiday and choose whether the DC is closed or
  operating. A closed Saturday skips the assignment and is excluded from
  coverage warnings; an open holiday is scheduled as a normal Saturday.
- When leave is recorded, upcoming automatic assignments are rebuilt using
  employees available that day and their recurring weekday rules. If fewer
  than two are available on an open Saturday, the calendar and dashboard warn
  that coverage cannot be met. Assigned employees are never scheduled on a
  recorded leave day; a replacement is chosen automatically where possible.
- Workload totals count Saturdays assigned during the visible calendar period.
  The dashboard distinguishes assigned Saturdays, Saturdays off, and actual
  Saturdays worked using attendance records. Automatic scheduling targets at
  least two Saturdays off per employee each month while keeping two employees
  assigned each open Saturday. In a normal four- or five-Saturday month, at
  least four available employees are needed to meet this rule. With fewer
  employees, restricted availability, or manual assignments that exceed the
  target, the scheduler keeps two-person coverage where possible, minimizes
  quota overruns, and warns that the minimum could not be met. Manual
  assignments remain respected unless invalid. Monthly workload remains the
  primary fairness measure; when tied, rotation favors employees with fewer
  assignments in the preceding 90 days, then uses actual attendance and the
  previous Saturday as tie-breakers.
- The workload dashboard separately warns when recorded actual Saturdays
  worked differ by more than one across active employees. This is distinct from
  assigned-roster balance because substitutes and other attendance overrides
  can change who actually worked.
- Export CSV for the visible period or use Print calendar and choose “Save as
  PDF” in the browser's print dialog.
- Activity history records roster edits, leave changes and decisions, holidays,
  manual assignments, attendance, and database restores. The panel displays
  paginated entries; older entries remain in the database. Since this starter
  has no login system, each entry uses the unverified “Local operator” label
  and cannot prove which person made the change. A restore is itself recorded
  in the restored database; the pre-restore data (and its history) is kept in
  the safety backup. The Activity History panel can search or filter by event
  date across all entries, displaying 20 changes per page. Use **Export CSV** to download the complete
  history, including older entries.
- On phone-sized screens the calendar opens in a readable agenda view; use
  **Grid** to switch to the month grid and **Agenda** to return.

The starter has no authentication; employee selection and request approval are
managed locally in the same app. It is intended to run on the local machine,
not be exposed directly to the public internet.

## Run tests

```powershell
python -m unittest discover -s tests
```
 added new line 