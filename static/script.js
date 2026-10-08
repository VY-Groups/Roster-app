const palette = ["#3577d4", "#e48a42", "#31a384", "#9b6dd1", "#d45f79", "#718096", "#c2a02f", "#3299aa"];
const weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const activityPageSize = 20;

document.addEventListener("DOMContentLoaded", () => {
  const calendarElement = document.getElementById("calendar");
  const dialog = document.getElementById("assignment-dialog");
  const renameDialog = document.getElementById("rename-dialog");
  const currentRole = document.body.dataset.userRole;
  const readOnly = currentRole === "viewer";
  const mobileCalendar = window.matchMedia("(max-width: 680px)").matches;
  let activityEntries = [];
  let activityPage = 1;
  let activityRequestId = 0;
  let activitySearchTimer;
  const calendar = new FullCalendar.Calendar(calendarElement, {
    initialView: mobileCalendar ? "listMonth" : "dayGridMonth",
    height: "auto",
    fixedWeekCount: false,
    firstDay: 1,
    buttonText: {
      dayGridMonth: "Grid",
      listMonth: "Agenda",
    },
    headerToolbar: {
      left: "prev,next today",
      center: "title",
      right: mobileCalendar ? "dayGridMonth,listMonth" : "",
    },
    events: "/api/calendar",
    eventContent: (info) => {
      if (info.event.extendedProps.coverage_issue || info.event.extendedProps.holiday) {
        const label = document.createElement("span");
        label.textContent = info.event.title;
        const content = document.createElement("span");
        content.append(label);
        appendLeaveLabel(content, info.event.extendedProps.leave_names);
        appendAttendanceLabel(content, info.event.extendedProps);
        return { domNodes: [content] };
      }
      const content = document.createElement("span");
      const names = info.event.extendedProps.employee_names || [];
      const ids = info.event.extendedProps.employee_ids || [];
      names.forEach((name, index) => {
        if (index) content.append(" + ");
        const person = document.createElement("span");
        const dot = document.createElement("span");
        dot.className = "event-employee-dot";
        dot.style.backgroundColor = colorFor(ids[index]);
        const label = document.createElement("span");
        label.textContent = name;
        person.append(dot, label);
        content.append(person);
      });
      appendLeaveLabel(content, info.event.extendedProps.leave_names);
      appendAttendanceLabel(content, info.event.extendedProps);
      return { domNodes: [content] };
    },
    dateClick: (info) => openAssignmentDialog(info.dateStr),
    eventClick: (info) => openAssignmentDialog(info.event.startStr),
    datesSet: () => refreshDashboard(),
  });
  calendar.render();

  if (currentRole !== "admin") {
    document.querySelector(".backup-panel").hidden = true;
  }
  if (readOnly) {
    [
      "#employee-form",
      "#employee-import-form",
      "#leave-import-form",
      "#leave-request-form",
      "#pair-exclusion-form",
    ].forEach((selector) => {
      const form = document.querySelector(selector);
      if (form) form.hidden = true;
    });
  }

  document.getElementById("employee-form").addEventListener("submit", addEmployee);
  document.getElementById("employee-import-form").addEventListener("submit", importEmployees);
  document.getElementById("pair-exclusion-form").addEventListener("submit", addPairExclusion);
  const accountForm = document.getElementById("account-form");
  if (accountForm) {
    accountForm.addEventListener("submit", createAccount);
    loadAccounts();
  }
  document.getElementById("employee-export-button").addEventListener("click", () => {
    window.location.href = "/api/employees/export.csv";
  });
  document.getElementById("leave-import-form").addEventListener("submit", importLeave);
  document.getElementById("what-if-form").addEventListener("submit", previewLeaveImpact);
  document.getElementById("roster-preview-form").addEventListener("submit", previewRosterChange);
  document.getElementById("roster-preview-action").addEventListener("change", updateRosterPreviewFields);
  document.getElementById("leave-request-form").addEventListener("submit", submitLeaveRequest);
  document.getElementById("rename-form").addEventListener("submit", saveEmployeeName);
  document.getElementById("cancel-rename").addEventListener("click", () => renameDialog.close());
  document.getElementById("save-assignment").addEventListener("click", saveAssignment);
  document.getElementById("clear-assignment-override").addEventListener("click", clearAssignmentOverride);
  document.getElementById("save-attendance").addEventListener("click", saveAttendance);
  document.getElementById("availability-employee").addEventListener("change", loadWeeklyAvailability);
  document.getElementById("save-availability").addEventListener("click", saveWeeklyAvailability);
  document.getElementById("save-holiday").addEventListener("click", saveHoliday);
  document.getElementById("remove-holiday").addEventListener("click", removeHoliday);
  document.getElementById("print-button").addEventListener("click", () => window.print());
  document.getElementById("export-button").addEventListener("click", exportCsv);
  document.getElementById("activity-export-button").addEventListener("click", () => {
    window.location.href = "/api/activity.csv";
  });
  document.getElementById("activity-search").addEventListener("input", () => {
    activityPage = 1;
    clearTimeout(activitySearchTimer);
    activitySearchTimer = setTimeout(loadActivity, 250);
  });
  document.getElementById("activity-date-filter").addEventListener("change", () => {
    activityPage = 1;
    loadActivity();
  });
  document.getElementById("activity-clear-filters").addEventListener("click", () => {
    document.getElementById("activity-search").value = "";
    document.getElementById("activity-date-filter").value = "";
    activityPage = 1;
    loadActivity();
  });
  document.getElementById("activity-previous").addEventListener("click", () => {
    if (activityPage > 1) {
      activityPage -= 1;
      loadActivity();
    }
  });
  document.getElementById("activity-next").addEventListener("click", () => {
    activityPage += 1;
    loadActivity();
  });
  document.getElementById("backup-button").addEventListener("click", () => {
    window.location.href = "/api/backup";
  });
  document.getElementById("restore-form").addEventListener("submit", restoreBackup);
  refreshDashboard();

  function appendLeaveLabel(content, leaveNames = []) {
    if (!leaveNames.length) return;
    content.append(document.createElement("br"));
    const leaveLabel = document.createElement("span");
    leaveLabel.className = "event-leave-label";
    leaveLabel.textContent = `On leave: ${leaveNames.join(", ")}`;
    content.append(leaveLabel);
  }

  function appendAttendanceLabel(content, props) {
    if (!props.attendance_recorded) return;
    content.append(document.createElement("br"));
    const attendanceLabel = document.createElement("span");
    attendanceLabel.className = "event-attendance-label";
    attendanceLabel.textContent = props.actual_names?.length
      ? `Worked: ${props.actual_names.join(", ")}`
      : "Attendance recorded (no employees selected)";
    content.append(attendanceLabel);
  }

  async function refreshDashboard() {
    activityPage = 1;
    await Promise.all([
      loadEmployees(),
      loadWorkload(),
      loadLeaveRequests(),
      loadActivity(),
    ]);
  }

  async function loadEmployees() {
    const employees = await api("/api/employees");
    const list = document.getElementById("employee-list");
    list.replaceChildren();
    document.getElementById("employee-total").textContent = employees.length;
    const requestEmployee = document.getElementById("leave-request-employee");
    const whatIfEmployee = document.getElementById("what-if-employee");
    const rosterPreviewEmployee = document.getElementById("roster-preview-employee");
    const pairFirstEmployee = document.getElementById("pair-first-employee");
    const pairSecondEmployee = document.getElementById("pair-second-employee");
    const previousRequestEmployee = requestEmployee.value;
    const previousWhatIfEmployee = whatIfEmployee.value;
    const previousRosterPreviewEmployee = rosterPreviewEmployee.value;
    requestEmployee.replaceChildren();
    whatIfEmployee.replaceChildren();
    rosterPreviewEmployee.replaceChildren();
    pairFirstEmployee.replaceChildren();
    pairSecondEmployee.replaceChildren();
    employees.forEach((employee) => {
      const option = document.createElement("option");
      option.value = employee.id;
      option.textContent = employee.name;
      requestEmployee.append(option);
      const whatIfOption = document.createElement("option");
      whatIfOption.value = employee.id;
      whatIfOption.textContent = employee.name;
      whatIfEmployee.append(whatIfOption);
      const rosterOption = document.createElement("option");
      rosterOption.value = employee.id;
      rosterOption.textContent = employee.name;
      rosterPreviewEmployee.append(rosterOption);
      [pairFirstEmployee, pairSecondEmployee].forEach((select) => {
        const pairOption = document.createElement("option");
        pairOption.value = employee.id;
        pairOption.textContent = employee.name;
        select.append(pairOption);
      });
      const item = document.createElement("li");
      item.className = "employee-item";
      const dot = document.createElement("span");
      dot.className = "employee-dot";
      dot.style.backgroundColor = colorFor(employee.id);
      const name = document.createElement("span");
      name.textContent = employee.name;
      const rename = document.createElement("button");
      rename.className = "rename-employee";
      rename.type = "button";
      rename.textContent = "Rename";
      rename.setAttribute("aria-label", `Rename ${employee.name}`);
      rename.addEventListener("click", () => openRenameDialog(employee));
      const remove = document.createElement("button");
      remove.className = "remove-employee";
      remove.type = "button";
      remove.textContent = "Remove";
      remove.setAttribute("aria-label", `Remove ${employee.name}`);
      remove.disabled = employees.length <= 3;
      remove.title = remove.disabled
        ? "At least three active employees are required."
        : `Remove ${employee.name} and rebalance upcoming Saturdays`;
      remove.addEventListener("click", () => removeEmployee(employee));
      item.append(dot, name, rename, remove);
      list.append(item);
    });
    if (employees.some((employee) => String(employee.id) === previousRequestEmployee)) {
      requestEmployee.value = previousRequestEmployee;
    }
    if (employees.some((employee) => String(employee.id) === previousWhatIfEmployee)) {
      whatIfEmployee.value = previousWhatIfEmployee;
    }
    if (employees.some((employee) => String(employee.id) === previousRosterPreviewEmployee)) {
      rosterPreviewEmployee.value = previousRosterPreviewEmployee;
    }
    const whatIfDate = document.getElementById("what-if-date");
    if (!whatIfDate.value) whatIfDate.value = nextSaturdayDate();
    const rosterPreviewDate = document.getElementById("roster-preview-date");
    if (!rosterPreviewDate.value) rosterPreviewDate.value = nextSaturdayDate();
    updateRosterPreviewFields();
    await loadPairExclusions();
  }

  async function loadPairExclusions() {
    const list = document.getElementById("pair-exclusion-list");
    const restrictions = await api("/api/pair-exclusions");
    list.replaceChildren();
    if (!restrictions.length) {
      const empty = document.createElement("li");
      empty.className = "muted small-copy";
      empty.textContent = "No pairing restrictions.";
      list.append(empty);
      return;
    }
    restrictions.forEach((restriction) => {
      const item = document.createElement("li");
      item.className = "pair-exclusion-item";
      const label = document.createElement("span");
      label.textContent =
        `${restriction.employee1_name} × ${restriction.employee2_name}`;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "button button-secondary";
      remove.textContent = "Remove";
      remove.addEventListener("click", async () => {
        try {
          await api(
            `/api/pair-exclusions/${restriction.employee1_id}/${restriction.employee2_id}`,
            { method: "DELETE" },
          );
          calendar.refetchEvents();
          await refreshDashboard();
          showToast("Pairing restriction removed; upcoming assignments rebalanced.");
        } catch (error) {
          showToast(error.message, true);
        }
      });
      item.append(label, remove);
      list.append(item);
    });
  }

  async function addPairExclusion(event) {
    event.preventDefault();
    const firstId = Number(document.getElementById("pair-first-employee").value);
    const secondId = Number(document.getElementById("pair-second-employee").value);
    const error = document.getElementById("pair-exclusion-error");
    error.textContent = "";
    if (!firstId || !secondId || firstId === secondId) {
      error.textContent = "Choose two different employees.";
      return;
    }
    try {
      await api("/api/pair-exclusions", {
        method: "POST",
        body: JSON.stringify({ employee_ids: [firstId, secondId] }),
      });
      calendar.refetchEvents();
      await refreshDashboard();
      showToast("Pairing restriction saved; upcoming assignments rebalanced.");
    } catch (requestError) {
      error.textContent = requestError.message;
    }
  }

  function updateRosterPreviewFields() {
    const adding = document.getElementById("roster-preview-action").value === "add";
    document.getElementById("roster-preview-name").hidden = !adding;
    document.querySelector("label[for='roster-preview-name']").hidden = !adding;
    document.getElementById("roster-preview-employee").hidden = adding;
    document.querySelector("label[for='roster-preview-employee']").hidden = adding;
    document.getElementById("roster-preview-name").required = adding;
    document.getElementById("roster-preview-employee").required = !adding;
  }

  async function previewRosterChange(event) {
    event.preventDefault();
    const result = document.getElementById("roster-preview-result");
    result.hidden = false;
    result.replaceChildren();
    const loading = document.createElement("p");
    loading.className = "muted small-copy";
    loading.textContent = "Calculating a read-only preview…";
    result.append(loading);
    const action = document.getElementById("roster-preview-action").value;
    const params = new URLSearchParams({
      action,
      date: document.getElementById("roster-preview-date").value,
    });
    if (action === "add") {
      params.set("name", document.getElementById("roster-preview-name").value.trim());
    } else {
      params.set("employee_id", document.getElementById("roster-preview-employee").value);
    }
    try {
      const preview = await api(`/api/simulate/roster?${params}`);
      result.replaceChildren();
      const activeCount = document.createElement("p");
      activeCount.className = preview.active_employee_count >= 3
        ? "preview-covered"
        : "preview-gap";
      activeCount.textContent =
        `Active employees after change: ${preview.active_employee_count}`;
      const scenarioDate = document.createElement("p");
      scenarioDate.textContent =
        `${action === "add" ? "Add" : "Remove"} ${preview.employee_name}, starting ${formatDate(preview.date)}.`;
      result.append(activeCount, scenarioDate);
      if (preview.coverage_gaps.length) {
        const warning = document.createElement("p");
        warning.className = "preview-gap";
        warning.textContent =
          `Coverage gaps: ${preview.coverage_gaps.map((gap) => `${formatDate(gap.date)} (${gap.available} available)`).join("; ")}.`;
        result.append(warning);
      } else if (preview.active_employee_count >= 3) {
        const covered = document.createElement("p");
        covered.className = "preview-covered";
        covered.textContent = "No uncovered open Saturdays in the 90-day preview.";
        result.append(covered);
      }
      const planHeading = document.createElement("strong");
      planHeading.textContent = "Upcoming Saturday teams over 90 days (current → preview)";
      const plans = document.createElement("ul");
      plans.className = "preview-workload";
      const beforeByDate = new Map(
        preview.assignments_before.map((assignment) => [assignment.date, assignment.employees]),
      );
      const afterByDate = new Map(
        preview.assignments_after.map((assignment) => [assignment.date, assignment.employees]),
      );
      const dates = [...new Set([...beforeByDate.keys(), ...afterByDate.keys()])].sort();
      dates.forEach((date) => {
        const item = document.createElement("li");
        item.textContent =
          `${formatDate(date)}: ${beforeByDate.get(date)?.join(" + ") || "No assignment"} → ${afterByDate.get(date)?.join(" + ") || "No assignment"}`;
        plans.append(item);
      });
      if (dates.length) result.append(planHeading, plans);
      const workloadHeading = document.createElement("strong");
      workloadHeading.textContent = "Scheduled Saturdays over 90 days (before → preview)";
      const workload = document.createElement("ul");
      workload.className = "preview-workload";
      preview.workload.forEach((employee) => {
        const item = document.createElement("li");
        item.textContent =
          `${employee.name}${employee.active_after ? "" : " (inactive)"}: ${employee.scheduled_before} → ${employee.scheduled_after}`;
        workload.append(item);
      });
      result.append(workloadHeading, workload);
    } catch (error) {
      result.replaceChildren();
      const message = document.createElement("p");
      message.className = "preview-gap";
      message.textContent = error.message;
      result.append(message);
    }
  }

  async function previewLeaveImpact(event) {
    event.preventDefault();
    const result = document.getElementById("what-if-result");
    result.hidden = false;
    result.replaceChildren();
    const loading = document.createElement("p");
    loading.className = "muted small-copy";
    loading.textContent = "Calculating a read-only preview…";
    result.append(loading);
    const params = new URLSearchParams({
      employee_id: document.getElementById("what-if-employee").value,
      date: document.getElementById("what-if-date").value,
    });
    try {
      const preview = await api(`/api/simulate/leave?${params}`);
      result.replaceChildren();
      const status = document.createElement("p");
      status.className = preview.coverage_met ? "preview-covered" : "preview-gap";
      status.textContent = preview.message;
      const dateLabel = document.createElement("p");
      dateLabel.textContent =
        `${preview.employee_name} hypothetically unavailable on ${formatDate(preview.date)}.`;
      const before = document.createElement("p");
      before.textContent =
        `Current plan: ${preview.scheduled_before.join(" + ") || "No assignment"}`;
      const after = document.createElement("p");
      after.textContent =
        `Preview plan: ${preview.scheduled_after.join(" + ") || "No assignment"}`;
      result.append(status, dateLabel, before, after);
      if (preview.workload.length) {
        const workloadHeading = document.createElement("strong");
        workloadHeading.textContent = "Monthly scheduled Saturdays (before → preview)";
        const workload = document.createElement("ul");
        workload.className = "preview-workload";
        preview.workload.forEach((employee) => {
          const item = document.createElement("li");
          item.textContent =
            `${employee.name}: ${employee.scheduled_before} → ${employee.scheduled_after}`;
          workload.append(item);
        });
        result.append(workloadHeading, workload);
      }
    } catch (error) {
      result.replaceChildren();
      const message = document.createElement("p");
      message.className = "preview-gap";
      message.textContent = error.message;
      result.append(message);
    }
  }

  async function loadLeaveRequests() {
    const requests = await api("/api/leave-requests?status=pending");
    const list = document.getElementById("leave-request-list");
    list.replaceChildren();
    if (!requests.length) {
      const empty = document.createElement("li");
      empty.className = "muted small-copy";
      empty.textContent = "No pending requests.";
      list.append(empty);
      return;
    }
    requests.forEach((leaveRequest) => {
      const item = document.createElement("li");
      item.className = "leave-request-item";
      const details = document.createElement("p");
      details.textContent = `${leaveRequest.employee_name}: ${formatDate(leaveRequest.start_date)} – ${formatDate(leaveRequest.end_date)}`;
      const forecast = leaveRequest.coverage_forecast;
      const forecastMessage = document.createElement("p");
      forecastMessage.className = forecast.coverage_risks.length
        ? "leave-forecast-risk"
        : "leave-forecast-ok";
      if (forecast.coverage_risks.length) {
        forecastMessage.textContent =
          `If approved, coverage may fall below two on: ${forecast.coverage_risks
            .map((risk) => `${formatDate(risk.date)} (${risk.available_after_approval} available)`)
            .join(", ")}.`;
      } else {
        forecastMessage.textContent = forecast.open_saturdays_considered
          ? `No coverage gap predicted for this request across ${forecast.open_saturdays_considered} open Saturday(s).`
          : "No future open Saturdays fall within this request.";
      }
      const actions = document.createElement("div");
      actions.className = "leave-request-actions";
      const approve = document.createElement("button");
      approve.className = "button button-primary";
      approve.type = "button";
      approve.textContent = "Approve";
      approve.addEventListener("click", () => reviewLeaveRequest(leaveRequest.id, "approved"));
      const reject = document.createElement("button");
      reject.className = "button button-secondary";
      reject.type = "button";
      reject.textContent = "Reject";
      reject.addEventListener("click", () => reviewLeaveRequest(leaveRequest.id, "rejected"));
      actions.append(approve, reject);
      item.append(details, forecastMessage, actions);
      list.append(item);
    });
  }

  async function loadAccounts() {
    const list = document.getElementById("account-list");
    if (!list) return;
    const users = await api("/api/users");
    list.replaceChildren();
    users.forEach((user) => {
      const item = document.createElement("li");
      item.className = "account-item";
      const identity = document.createElement("span");
      identity.textContent = `${user.username} · ${user.role}${user.active ? "" : " · inactive"}`;
      const role = document.createElement("select");
      role.setAttribute("aria-label", `Role for ${user.username}`);
      ["admin", "operator", "viewer"].forEach((roleName) => {
        const option = document.createElement("option");
        option.value = roleName;
        option.textContent = roleName;
        role.append(option);
      });
      role.value = user.role;
      role.addEventListener("change", async () => {
        try {
          await api(`/api/users/${user.id}`, {
            method: "PATCH",
            body: JSON.stringify({ role: role.value }),
          });
          await loadAccounts();
          showToast(`Updated ${user.username}'s role.`);
        } catch (error) {
          showToast(error.message, true);
          role.value = user.role;
        }
      });
      const activeButton = document.createElement("button");
      activeButton.type = "button";
      activeButton.className = "button button-secondary";
      activeButton.textContent = user.active ? "Deactivate" : "Reactivate";
      activeButton.disabled = user.id === Number(document.body.dataset.userId);
      activeButton.addEventListener("click", async () => {
        try {
          await api(`/api/users/${user.id}`, {
            method: "PATCH",
            body: JSON.stringify({ active: !user.active }),
          });
          await loadAccounts();
          showToast(`${user.username} ${user.active ? "deactivated" : "reactivated"}.`);
        } catch (error) {
          showToast(error.message, true);
        }
      });
      item.append(identity, role, activeButton);
      list.append(item);
    });
  }

  async function createAccount(event) {
    event.preventDefault();
    const errorElement = document.getElementById("account-error");
    errorElement.textContent = "";
    try {
      await api("/api/users", {
        method: "POST",
        body: JSON.stringify({
          username: document.getElementById("account-username").value,
          password: document.getElementById("account-password").value,
          role: document.getElementById("account-role").value,
        }),
      });
      document.getElementById("account-form").reset();
      await loadAccounts();
      showToast("Account created.");
    } catch (error) {
      errorElement.textContent = error.message;
    }
  }

  async function loadActivity() {
    const requestId = ++activityRequestId;
    const params = new URLSearchParams({
      page: String(activityPage),
      page_size: String(activityPageSize),
      search: document.getElementById("activity-search").value.trim(),
      date: document.getElementById("activity-date-filter").value,
    });
    const result = await api(`/api/activity?${params}`);
    if (requestId !== activityRequestId) return;
    activityEntries = result.entries;
    const list = document.getElementById("activity-list");
    list.replaceChildren();
    if (!activityEntries.length) {
      const empty = document.createElement("li");
      empty.className = "muted small-copy";
      empty.textContent = result.total
        ? "No activity matches these filters."
        : "No changes recorded yet.";
      list.append(empty);
    } else {
      activityEntries.forEach((entry) => {
        const item = document.createElement("li");
        item.className = "activity-item";
        const heading = document.createElement("strong");
        heading.textContent = entry.employee_name
          ? `${entry.action} · ${entry.employee_name}`
          : entry.action;
        const meta = document.createElement("span");
        const timestamp = new Date(`${entry.occurred_at.replace(" ", "T")}Z`);
        meta.textContent = `${entry.actor} · ${timestamp.toLocaleString()}${entry.date ? ` · ${formatDate(entry.date)}` : ""}`;
        const details = document.createElement("p");
        details.textContent = entry.details;
        item.append(heading, meta);
        if (entry.details) item.append(details);
        list.append(item);
      });
    }
    const firstResult = result.total ? (result.page - 1) * result.page_size + 1 : 0;
    const lastResult = Math.min(result.page * result.page_size, result.total);
    document.getElementById("activity-result-count").textContent =
      `${firstResult}–${lastResult} of ${result.total} matching changes`;
    document.getElementById("activity-page-status").textContent =
      `Page ${result.total_pages ? result.page : 0} of ${result.total_pages}`;
    document.getElementById("activity-previous").disabled = result.page <= 1;
    document.getElementById("activity-next").disabled =
      result.page >= result.total_pages;
    activityPage = result.page || 1;
  }

  async function submitLeaveRequest(event) {
    event.preventDefault();
    const error = document.getElementById("leave-request-error");
    error.textContent = "";
    try {
      await api("/api/leave-requests", {
        method: "POST",
        body: JSON.stringify({
          employee_id: Number(document.getElementById("leave-request-employee").value),
          start_date: document.getElementById("leave-request-start").value,
          end_date: document.getElementById("leave-request-end").value,
        }),
      });
      document.getElementById("leave-request-form").reset();
      await refreshDashboard();
      showToast("Leave request submitted; it will not affect the roster until approved.");
    } catch (requestError) {
      error.textContent = requestError.message;
    }
  }

  async function reviewLeaveRequest(requestId, status) {
    try {
      await api(`/api/leave-requests/${requestId}`, {
        method: "PATCH",
        body: JSON.stringify({ status }),
      });
      calendar.refetchEvents();
      await refreshDashboard();
      showToast(status === "approved"
        ? "Leave approved; upcoming Saturdays rebalanced."
        : "Leave request rejected.");
    } catch (error) {
      showToast(error.message, true);
    }
  }

  async function restoreBackup(event) {
    event.preventDefault();
    const error = document.getElementById("restore-error");
    error.textContent = "";
    const fileInput = document.getElementById("restore-file");
    if (!fileInput.files.length) {
      error.textContent = "Choose a SQLite backup file.";
      return;
    }
    if (!document.getElementById("restore-confirm").checked) {
      error.textContent = "Confirm that you understand this replaces the current roster.";
      return;
    }
    if (!window.confirm(
      "Replace all current roster data with this backup? A safety backup of the current database will be kept."
    )) {
      return;
    }

    const submit = document.querySelector("#restore-form button[type='submit']");
    submit.disabled = true;
    try {
      const formData = new FormData();
      formData.append("backup", fileInput.files[0]);
      formData.append("confirm_replace", "true");
      const response = await fetch("/api/restore", {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken() },
        body: formData,
      });
      const result = await response.json();
      if (!response.ok) {
        throw new Error(result.error || "The backup could not be restored.");
      }
      showToast(`Backup restored. Safety copy saved as ${result.safety_backup}.`);
      setTimeout(() => window.location.reload(), 1300);
    } catch (restoreError) {
      error.textContent = restoreError.message;
      submit.disabled = false;
    }
  }

  function formatDate(value) {
    return new Date(`${value}T00:00:00`).toLocaleDateString(undefined, {
      year: "numeric", month: "short", day: "numeric",
    });
  }

  async function loadWorkload() {
    const view = calendar.view;
    const summary = await api(`/api/summary?start=${localDateString(view.currentStart)}&end=${localDateString(view.currentEnd)}`);
    const workload = document.getElementById("workload-list");
    workload.replaceChildren();
    const max = Math.max(1, ...summary.employees.map((employee) => employee.saturday_count));
    summary.employees.forEach((employee) => {
      const row = document.createElement("div");
      row.className = "workload-row";
      const label = document.createElement("div");
      label.className = "workload-label";
      const name = document.createElement("span");
      name.textContent = employee.name;
      const count = document.createElement("strong");
      count.textContent = `Assigned ${employee.saturday_count} · Off ${employee.saturdays_off_count} · Worked ${employee.worked_count}`;
      count.setAttribute(
        "aria-label",
        `${employee.saturday_count} assigned Saturdays, ${employee.saturdays_off_count} Saturdays off, ${employee.worked_count} Saturdays worked`,
      );
      label.append(name, count);
      const track = document.createElement("div");
      track.className = "workload-track";
      const bar = document.createElement("span");
      bar.style.width = `${(employee.saturday_count / max) * 100}%`;
      bar.style.backgroundColor = colorFor(employee.id);
      track.append(bar);
      row.append(label, track);
      workload.append(row);
    });
    const alert = document.getElementById("balance-alert");
    alert.hidden = !summary.warning;
    alert.textContent = summary.warning || "";
    const actualWorkAlert = document.getElementById("actual-work-alert");
    actualWorkAlert.hidden = !summary.actual_work_warning;
    actualWorkAlert.textContent = summary.actual_work_warning || "";
    document.getElementById("calendar-hint").textContent =
      summary.active_employee_count < summary.minimum_employee_count
        ? `Add ${summary.minimum_employee_count - summary.active_employee_count} more active employee(s) to meet the three-person minimum.`
        : "Select a date to record a holiday or recurring unavailable days. On Saturdays, you can also adjust the team or record leave.";
  }

  async function addEmployee(event) {
    event.preventDefault();
    const input = document.getElementById("employee-name");
    try {
      await api("/api/employees", { method: "POST", body: JSON.stringify({ name: input.value }) });
      input.value = "";
      calendar.refetchEvents();
      await refreshDashboard();
      showToast("Employee added.");
    } catch (error) {
      showToast(error.message, true);
    }
  }

  async function importEmployees(event) {
    event.preventDefault();
    const form = document.getElementById("employee-import-form");
    const fileInput = document.getElementById("employee-import-file");
    const message = document.getElementById("employee-import-message");
    message.textContent = "";
    if (!fileInput.files.length) {
      message.textContent = "Choose a CSV file.";
      return;
    }
    const submit = form.querySelector("button[type='submit']");
    submit.disabled = true;
    try {
      const formData = new FormData();
      formData.append("file", fileInput.files[0]);
      const response = await fetch("/api/employees/import", {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken() },
        body: formData,
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "The CSV import failed.");
      calendar.refetchEvents();
      await refreshDashboard();
      message.textContent =
        `Added ${result.added.length}, reactivated ${result.reactivated.length}, skipped ${result.skipped.length} existing employee(s).`;
      fileInput.value = "";
    } catch (error) {
      message.textContent = error.message;
    } finally {
      submit.disabled = false;
    }
  }

  async function importLeave(event) {
    event.preventDefault();
    const form = document.getElementById("leave-import-form");
    const fileInput = document.getElementById("leave-import-file");
    const message = document.getElementById("leave-import-message");
    message.textContent = "";
    if (!fileInput.files.length) {
      message.textContent = "Choose a CSV file.";
      return;
    }
    const submit = form.querySelector("button[type='submit']");
    submit.disabled = true;
    try {
      const formData = new FormData();
      formData.append("file", fileInput.files[0]);
      const response = await fetch("/api/leave/import", {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken() },
        body: formData,
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "The leave import failed.");
      calendar.refetchEvents();
      await refreshDashboard();
      message.textContent =
        `Added ${result.added.length} leave day(s); skipped ${result.skipped.length} duplicate or existing row(s).`;
      fileInput.value = "";
    } catch (error) {
      message.textContent = error.message;
    } finally {
      submit.disabled = false;
    }
  }

  async function removeEmployee(employee) {
    if (!window.confirm(`Remove ${employee.name} from the active roster? Upcoming automatic assignments will be rebalanced.`)) {
      return;
    }
    try {
      await api(`/api/employees/${employee.id}`, { method: "DELETE" });
      calendar.refetchEvents();
      await refreshDashboard();
      showToast(`${employee.name} removed and upcoming automatic assignments rebalanced.`);
    } catch (error) {
      showToast(error.message, true);
    }
  }

  function openRenameDialog(employee) {
    renameDialog.dataset.employeeId = employee.id;
    document.getElementById("rename-name").value = employee.name;
    document.getElementById("rename-error").textContent = "";
    renameDialog.showModal();
    document.getElementById("rename-name").focus();
    document.getElementById("rename-name").select();
  }

  async function saveEmployeeName(event) {
    event.preventDefault();
    const input = document.getElementById("rename-name");
    const name = input.value.trim();
    const error = document.getElementById("rename-error");
    if (!name) {
      error.textContent = "Enter an employee name.";
      return;
    }
    try {
      await api(`/api/employees/${renameDialog.dataset.employeeId}`, {
        method: "PATCH",
        body: JSON.stringify({ name }),
      });
      renameDialog.close();
      calendar.refetchEvents();
      await refreshDashboard();
      showToast("Employee name updated.");
    } catch (error) {
      document.getElementById("rename-error").textContent = error.message;
    }
  }

  async function openAssignmentDialog(day) {
    const employees = await api("/api/employees");
    const isSaturday = new Date(`${day}T00:00:00`).getDay() === 6;
    const events = isSaturday
      ? await api(`/api/calendar?start=${day}&end=${nextDay(day)}`)
      : [];
    const [leave, holidayResponse, attendance, substituteData] = await Promise.all([
      isSaturday ? api(`/api/leave?date=${encodeURIComponent(day)}`) : Promise.resolve({ employee_ids: [] }),
      api(`/api/holidays?date=${encodeURIComponent(day)}`),
      isSaturday
        ? api(`/api/attendance?date=${encodeURIComponent(day)}`)
        : Promise.resolve({ recorded: false, employee_ids: [], employees: [] }),
      isSaturday
        ? api(`/api/substitutes/${encodeURIComponent(day)}`)
        : Promise.resolve({ assigned_employee_ids: [], substitutes: [] }),
    ]);
    const scheduledEvent = events.find((event) => !event.extendedProps.coverage_issue && !event.extendedProps.holiday);
    const assigned = scheduledEvent?.extendedProps.employee_ids || [];
    document.getElementById("dialog-date").textContent = new Date(`${day}T00:00:00`).toLocaleDateString(undefined, {
      weekday: "long", year: "numeric", month: "long", day: "numeric",
    });
    document.getElementById("assignment-dialog").dataset.date = day;
    const belowMinimum = employees.length < 3;
    document.getElementById("assignment-controls").hidden = !isSaturday;
    document.getElementById("dialog-error").textContent = belowMinimum
      ? "At least three active employees are required before scheduling."
      : "";
    document.getElementById("save-assignment").disabled = belowMinimum;
    const clearOverride = document.getElementById("clear-assignment-override");
    clearOverride.hidden = !isSaturday || !scheduledEvent?.extendedProps.manual ||
      day < localDateString(new Date());
    document.getElementById("clear-assignment-error").textContent = "";
    const attendanceOptions = document.getElementById("attendance-options");
    attendanceOptions.replaceChildren();
    const attendanceEmployees = [...employees];
    attendance.employees.forEach((attendee) => {
      if (!attendanceEmployees.some((employee) => employee.id === attendee.id)) {
        attendanceEmployees.push(attendee);
      }
    });
    attendanceEmployees.forEach((employee) => {
      const label = document.createElement("label");
      label.className = "leave-option";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = employee.id;
      checkbox.checked = attendance.employee_ids.includes(employee.id);
      const text = document.createElement("span");
      text.textContent = employee.active === false
        ? `${employee.name} (inactive; recorded attendee)`
        : employee.name;
      label.append(checkbox, text);
      attendanceOptions.append(label);
    });
    document.getElementById("attendance-status").textContent = attendance.recorded
      ? "Attendance has been recorded for this Saturday."
      : "Attendance has not been recorded yet.";
    const assignmentOptions = document.getElementById("assignment-options");
    assignmentOptions.replaceChildren();
    employees.forEach((employee) => {
      const label = document.createElement("label");
      label.className = "option-row";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = employee.id;
      checkbox.checked = assigned.includes(employee.id);
      checkbox.disabled = belowMinimum || leave.employee_ids.includes(employee.id);
      checkbox.addEventListener("change", () => {
        const checked = assignmentOptions.querySelectorAll("input:checked");
        if (checked.length > 2) checkbox.checked = false;
      });
      const dot = document.createElement("span");
      dot.className = "employee-dot";
      dot.style.backgroundColor = colorFor(employee.id);
      const text = document.createElement("span");
      text.textContent = employee.name;
      label.append(checkbox, dot, text);
      assignmentOptions.append(label);
    });
    const substitutePanel = document.getElementById("substitute-panel");
    substitutePanel.hidden = !isSaturday;
    const substituteOut = document.getElementById("substitute-out");
    substituteOut.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Choose an assigned employee";
    substituteOut.append(placeholder);
    employees
      .filter((employee) => substituteData.assigned_employee_ids.includes(employee.id))
      .forEach((employee) => {
        const option = document.createElement("option");
        option.value = employee.id;
        option.textContent = employee.name;
        substituteOut.append(option);
      });
    const substituteList = document.getElementById("substitute-list");
    substituteList.replaceChildren();
    if (!substituteData.substitutes.length) {
      const empty = document.createElement("li");
      empty.className = "muted small-copy";
      empty.textContent = "No available substitutes for this Saturday.";
      substituteList.append(empty);
    } else {
      substituteData.substitutes.forEach((substitute) => {
        const item = document.createElement("li");
        item.className = "substitute-item";
        const details = document.createElement("span");
        const recent = substitute.worked_previous_saturday
          ? "; worked last Saturday"
          : "";
        details.textContent =
          `${substitute.name} · ${substitute.assignments_this_month} assigned, ${substitute.worked_this_month} worked this month${recent}`;
        const choose = document.createElement("button");
        choose.type = "button";
        choose.className = "button button-secondary";
        choose.textContent = "Select";
        choose.disabled = belowMinimum;
        choose.addEventListener("click", () => {
          const replacedId = Number(substituteOut.value);
          const replaced = assignmentOptions.querySelector(
            `input[value="${replacedId}"]`,
          );
          const replacement = assignmentOptions.querySelector(
            `input[value="${substitute.id}"]`,
          );
          if (!replacedId || !replaced?.checked || !replacement || replacement.disabled) {
            document.getElementById("dialog-error").textContent =
              "Choose an assigned employee to replace, then select an available substitute.";
            return;
          }
          const retained = [...assignmentOptions.querySelectorAll("input:checked")]
            .filter((input) => Number(input.value) !== replacedId);
          if (
            retained.some((input) =>
              substitute.incompatible_with_ids.includes(Number(input.value)),
            )
          ) {
            document.getElementById("dialog-error").textContent =
              `${substitute.name} cannot be paired with the employee who would remain assigned.`;
            return;
          }
          replaced.checked = false;
          replacement.checked = true;
          document.getElementById("dialog-error").textContent =
            `${substitute.name} is selected as the replacement. Save assignment to apply it.`;
        });
        item.append(details, choose);
        substituteList.append(item);
      });
    }
    const leaveOptions = document.getElementById("leave-options");
    leaveOptions.replaceChildren();
    employees.forEach((employee) => {
      const label = document.createElement("label");
      label.className = "leave-option";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = employee.id;
      checkbox.checked = leave.employee_ids.includes(employee.id);
      checkbox.disabled = belowMinimum;
      checkbox.addEventListener("change", async () => {
        try {
          const route = checkbox.checked ? "/api/leave" : "/api/leave";
          await api(route, {
            method: checkbox.checked ? "POST" : "DELETE",
            body: JSON.stringify({ employee_id: employee.id, date: day }),
          });
          await openAssignmentDialog(day);
          calendar.refetchEvents();
          await refreshDashboard();
        } catch (error) {
          checkbox.checked = !checkbox.checked;
          showToast(error.message, true);
        }
      });
      const text = document.createElement("span");
      text.textContent = checkbox.checked
        ? `${employee.name} is on leave — uncheck if they worked`
        : `${employee.name} is on leave`;
      label.append(checkbox, text);
      leaveOptions.append(label);
    });
    const availabilityEmployee = document.getElementById("availability-employee");
    const previouslySelected = Number(availabilityEmployee.value);
    availabilityEmployee.replaceChildren();
    employees.forEach((employee) => {
      const option = document.createElement("option");
      option.value = employee.id;
      option.textContent = employee.name;
      availabilityEmployee.append(option);
    });
    if (employees.some((employee) => employee.id === previouslySelected)) {
      availabilityEmployee.value = previouslySelected;
    }
    await loadWeeklyAvailability();
    applyReadOnlyPlanner();

    const holiday = holidayResponse.holiday;
    document.getElementById("holiday-enabled").checked = Boolean(holiday);
    document.getElementById("holiday-name").value = holiday?.name || "";
    document.getElementById("holiday-closed").checked = holiday?.dc_closed ?? true;
    document.getElementById("holiday-error").textContent = "";
    document.getElementById("remove-holiday").disabled = !holiday;
    if (!dialog.open) dialog.showModal();
  }

  async function saveAssignment() {
    if (!document.getElementById("assignment-controls").hidden &&
        document.getElementById("save-assignment").disabled) {
      return;
    }
    const day = dialog.dataset.date;
    const selected = [...document.querySelectorAll("#assignment-options input:checked")]
      .map((checkbox) => Number(checkbox.value));
    if (selected.length !== 2) {
      document.getElementById("dialog-error").textContent = "Select exactly two employees.";
      return;
    }
    try {
      await api(`/api/assignments/${day}`, {
        method: "POST",
        body: JSON.stringify({ employee_ids: selected }),
      });
      dialog.close();
      calendar.refetchEvents();
      await refreshDashboard();
      showToast("Saturday assignment saved.");
    } catch (error) {
      document.getElementById("dialog-error").textContent = error.message;
    }
  }

  async function clearAssignmentOverride() {
    const day = dialog.dataset.date;
    const error = document.getElementById("clear-assignment-error");
    error.textContent = "";
    if (!window.confirm("Clear the manual override and let automatic rotation choose this Saturday's team?")) {
      return;
    }
    try {
      const result = await api(`/api/assignments/${day}`, { method: "DELETE" });
      calendar.refetchEvents();
      await refreshDashboard();
      await openAssignmentDialog(day);
      showToast(
        result.employee_names.length
          ? `Returned to automatic rotation: ${result.employee_names.join(" + ")}.`
          : "Manual override cleared; the DC is closed that Saturday.",
      );
    } catch (requestError) {
      error.textContent = requestError.message;
    }
  }

  async function saveAttendance() {
    const day = dialog.dataset.date;
    const employeeIds = [...document.querySelectorAll("#attendance-options input:checked")]
      .map((checkbox) => Number(checkbox.value));
    try {
      const result = await api(`/api/attendance/${day}`, {
        method: "PUT",
        body: JSON.stringify({ employee_ids: employeeIds }),
      });
      await openAssignmentDialog(day);
      calendar.refetchEvents();
      await refreshDashboard();
      showToast(
        result.leave_canceled?.length
          ? "Attendance saved; leave canceled and upcoming shifts rebalanced."
          : "Actual attendance saved."
      );
    } catch (error) {
      showToast(error.message, true);
    }
  }

  async function loadWeeklyAvailability() {
    const employeeId = document.getElementById("availability-employee").value;
    const container = document.getElementById("weekday-options");
    container.replaceChildren();
    if (!employeeId) return;
    const result = await api(`/api/employees/${employeeId}/availability`);
    weekdays.forEach((day, weekday) => {
      const label = document.createElement("label");
      label.className = "weekday-option";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = weekday;
      checkbox.checked = result.weekdays.includes(weekday);
      const text = document.createElement("span");
      text.textContent = day;
      label.append(checkbox, text);
      container.append(label);
    });
    if (readOnly) {
      container.querySelectorAll("input").forEach((checkbox) => {
        checkbox.disabled = true;
      });
    }
  }

  function applyReadOnlyPlanner() {
    if (!readOnly) return;
    document.querySelectorAll(
      "#assignment-options input, #attendance-options input, #leave-options input",
    ).forEach((input) => {
      input.disabled = true;
    });
    [
      "#save-assignment",
      "#clear-assignment-override",
      "#save-attendance",
      "#save-availability",
      "#save-holiday",
      "#remove-holiday",
    ].forEach((selector) => {
      document.querySelector(selector).hidden = true;
    });
  }

  async function saveWeeklyAvailability() {
    const employeeId = document.getElementById("availability-employee").value;
    if (!employeeId) return;
    const weekdaysUnavailable = [...document.querySelectorAll("#weekday-options input:checked")]
      .map((checkbox) => Number(checkbox.value));
    try {
      await api(`/api/employees/${employeeId}/availability`, {
        method: "PUT",
        body: JSON.stringify({ weekdays: weekdaysUnavailable }),
      });
      calendar.refetchEvents();
      await refreshDashboard();
      showToast("Weekly availability saved; upcoming shifts were rebalanced.");
    } catch (error) {
      showToast(error.message, true);
    }
  }

  async function saveHoliday() {
    const day = dialog.dataset.date;
    const enabled = document.getElementById("holiday-enabled").checked;
    const errorElement = document.getElementById("holiday-error");
    errorElement.textContent = "";
    try {
      if (enabled) {
        await api(`/api/holidays/${day}`, {
          method: "PUT",
          body: JSON.stringify({
            name: document.getElementById("holiday-name").value,
            dc_closed: document.getElementById("holiday-closed").checked,
          }),
        });
      } else {
        await api(`/api/holidays/${day}`, { method: "DELETE" });
      }
      await openAssignmentDialog(day);
      calendar.refetchEvents();
      await refreshDashboard();
      showToast(enabled ? "Holiday saved." : "Holiday removed.");
    } catch (error) {
      errorElement.textContent = error.message;
    }
  }

  async function removeHoliday() {
    const day = dialog.dataset.date;
    try {
      await api(`/api/holidays/${day}`, { method: "DELETE"       });
      await openAssignmentDialog(day);
      calendar.refetchEvents();
      await refreshDashboard();
      showToast("Holiday removed.");
    } catch (error) {
      document.getElementById("holiday-error").textContent = error.message;
    }
  }

  function exportCsv() {
    const view = calendar.view;
    window.location.href = `/api/export.csv?start=${localDateString(view.currentStart)}&end=${localDateString(view.currentEnd)}`;
  }

  async function api(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken(),
        ...(options.headers || {}),
      },
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "The request could not be completed.");
    return data;
  }

  function csrfToken() {
    return document.querySelector("meta[name='csrf-token']").content;
  }

  function colorFor(id) {
    return palette[(id - 1) % palette.length];
  }

  function nextDay(day) {
    const date = new Date(`${day}T00:00:00`);
    date.setDate(date.getDate() + 1);
    return localDateString(date);
  }

  function nextSaturdayDate() {
    const day = new Date();
    day.setDate(day.getDate() + ((6 - day.getDay() + 7) % 7));
    return localDateString(day);
  }

  function localDateString(value) {
    const year = value.getFullYear();
    const month = String(value.getMonth() + 1).padStart(2, "0");
    const day = String(value.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  let toastTimer;
  function showToast(message, error = false) {
    const toast = document.getElementById("toast");
    toast.textContent = message;
    toast.classList.toggle("toast-error", error);
    toast.classList.add("toast-visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("toast-visible"), 3200);
  }
});
