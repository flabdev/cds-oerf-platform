# OERF dashboard

The dashboard is a local React page for the Operational Exception Resolution Framework. It shows cold-chain incidents, the ranked recovery options for a shipment that is being assessed, and the actions a desk user can queue.

This page reads the sample list in `src/App.jsx`. Refreshing the browser restores that list. It does not call the dashboard API, DynamoDB, or the in-memory incident store. Allowance publish and the action buttons only set a notice on the page. The HTTP contract those buttons would use is described in `docs/interfaces.md`.

## Setup

You need Node.js 18 or newer and npm.

From the repository root:

```powershell
cd web
npm install
npm run dev
```

Vite prints a local URL, usually `http://127.0.0.1:5173/`. Open that URL in a browser. Stop the server with Ctrl+C in the same terminal.

Other scripts:

```powershell
npm test
npm run build
```

`npm test` runs `src/model.test.js` with Node’s test runner. `npm run build` writes a static bundle to `web/dist/`. `node_modules/` and `dist/` are not committed.

## What you see

The page fills the browser window in three columns. Below 760px the columns stack.

- **Left rail.** Profile initial, display name, and tenant. Desk links for Incidents, Allowance, and Actions. The footer has an info icon, a collapse control, and the OERF wordmark. Collapsing the rail hides the labels.
- **Middle pane.** Search and the incident list, the allowance editor, or the action queue, depending on the Desk link.
- **Right pane.** The selected shipment: state, minutes of allowance left, evidence, recommendation, a temperature sketch, facility rows, timeline, and the same three actions as the queue.

The info icon opens a short description of the site. Close it with the × or by clicking the dimmed area around the dialog.

The temperature line is a fixed sketch. It is the same shape for every shipment.

## Sample shipments

| Shipment | State | What the right pane shows |
| --- | --- | --- |
| PH-2841 | ASSESSING | Facility A at 20.8 min, feasible. Facility B at 35.8 min, feasible. Recommendation is Facility A. Evidence is fault code E17. |
| VX-3101 | OPEN | No facility rows. Ranking has not run. |
| SBX-0001 | DIAGNOSING | No facility rows. Evidence is fault code E05. |

Search matches the shipment id, incident id, or state. The default selection is PH-2841.

## Desk screens

**Incidents.** Lists the three sample shipments and how many minutes of allowance remain.

**Actions.** Queues Confirm transfer, Pick option, or Cancel for the shipment that is selected in Incidents. Pick option uses that shipment’s recommendation. The buttons are disabled until a shipment is selected. A queued action shows a notice and does not change the shipment’s state.

**Allowance.** A tenant admin on tenant `t2` can type a minute value and press Publish. The notice says whether facility B is inside or outside that allowance. The sample rows on the page stay as they are. Any other role, or tenant `t1`, sees that editing is limited to a tenant admin on `t2`.

The three buttons at the bottom of the shipment pane queue the same actions.

## Session

The page does not sign in to Cognito. The query string sets the local session:

| URL | Session |
| --- | --- |
| `http://127.0.0.1:5173/` | Tenant admin on `t2`. The rail shows “Tenant admin”. |
| `?role=platform_admin` | Platform admin. A tenant list for `t1` and `t2` appears in the rail. |
| `?tenant=t2&role=tenant_admin` | Same as the default. |
| `?role=ops_user` | Ops user. Allowance editing stays closed. |
| `?name=Shift%20lead` | Shows that display name instead of the role label. |
| `?signedOut=1` | Guest. The incident list asks you to sign in. |

A blank `name` uses the role label. Guest is the signed-out label.

## Files

| File | Role |
| --- | --- |
| `index.html` | Page shell. Loads the Inter font. |
| `src/main.jsx` | Reads the query string and mounts the app. |
| `src/App.jsx` | Layout, sample incidents, and the three desk screens. |
| `src/model.js` | Display name, list states, action request shape, and the allowance note. |
| `src/styles.css` | Layout and color. |
| `vite.config.js` | Vite with the React plugin. |
