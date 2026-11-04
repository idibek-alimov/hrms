# HRMS project guidance

## Scope and ownership

This customized app owns HR and payroll behavior, employee self-service, and rostering.
Its users include employees, HR staff, payroll operators, and managers; identify the role
and first business question before designing a screen. Do not expose manager/payroll
information simply because an employee can open the app.

This directory is a separate Git repository in a Frappe bench. Use its current status
and customized `origin`, preserve unrelated edits, and do not replace it with upstream
source. A request to edit local files does not authorize committing, pushing, migrating,
paying employees, or deploying.

## Code and frontend map

- `hrms/hr/`: employee-related HR records and workflows.
- `hrms/payroll/`: salary structures, slips, payroll processing, and related accounting.
- `hrms/hooks.py`, `hrms/overrides/`: integration with ERPNext documents and lifecycle hooks.
- `frontend/`: employee-facing Vue/Ionic application using its existing Frappe UI dependencies.
- `roster/`: Vue roster application; it has its own build and route output.

Read the route, controller, hooks, and DocType owner before editing. In this bench,
custom staff hubs and some Employee behavior also live in ERPNext. Read its `AGENTS.md`
when those paths are affected; do not move them merely to make app boundaries look neater.

## UI direction

Carry forward the owner's approved simple, professional ERP style: compact identity,
plain language, quiet surfaces, legible tables, a few relevant figures, and clear actions.
Adapt it to HR tasks such as pending approvals, leave balances, attendance, and payroll
status rather than copying product stock or profit KPIs.

Use native Desk controls for Desk pages and the existing Vue/Ionic/Frappe UI components
for the employee app and roster. Preserve Russian/Tajik wording, dates, number/currency
formatting, keyboard access, mobile usability, and meaningful status text. Scope styles
and avoid decorative cards, badges, or large imagery that obscure a task.

Keep employee self-service distinct from manager actions. Provide relevant date/company/
employee filters and explicit loading, empty, error/retry, and permission-limited states.
Reject stale responses and preserve useful navigation context. Link to authorized native
records and keep advanced detail available progressively.

## HR and payroll invariants

- Preserve employee, company, reporting-manager, and role-level permissions on the server.
  Salary, bank, identity, and medical/leave details must not leak through aggregate APIs,
  exports, logs, broad queries, or a browser-only visibility rule.
- Follow native approval workflows, document states, validations, and audit trails.
  Opening a summary must not approve leave, generate/submit payroll, or issue payments.
- Keep leave allocation/consumption, holidays, half-days, attendance, shift boundaries,
  time zones, overnight shifts, and cancellation effects consistent with the source records.
- For payroll work, trace the applicable salary structure, effective dates, payable days,
  earnings/deductions, advances, precision, currency, and linked accounting/payment entries.
  Do not calculate a second independent payroll total solely for a dashboard.
- Label gross pay, deductions, net pay, accrued, paid, and outstanding distinctly.
  Do not import ERPNext product definitions of gross profit into payroll summaries.
- Reconcile summaries and detail at the same period/company/employee scope. Separate
  current balances from period activity, and keep filtered totals independent of pagination.
- Batch queries and use existing calculation services. Preserve hooks and cross-app
  contracts when simplifying the visible workflow.

## Verification and handoff

Test affected calculations, approval permissions, date boundaries, and cancellation
paths in proportion to the change, using suitable test data. Do not use live payroll
submission or real payments as verification. Inspect desktop/mobile views and relevant
Russian/Tajik labels when the UI changes.

Use the parent bench's Python environment and scoped Frappe test modules on a test site.
Inspect package scripts rather than assuming a `test` or `lint` command exists. From this
app root, `yarn build-pwa` builds `frontend/`, `yarn build-roster` builds `roster/`, and
`yarn build` builds both. Build the changed frontend and applicable bench assets as needed;
do not reinstall dependencies or build unrelated frontends for a documentation-only edit.

Report the changed workflow, linked ERPNext/accounting effects, actual checks, and any
unperformed build, restart, or operator action. Label fixtures and partial verification honestly.
