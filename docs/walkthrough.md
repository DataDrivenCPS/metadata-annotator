# Walkthrough: build and correct a model

Uses the sample project (`samples/ro-train/model.ttl`): a brackish-water RO train with three
deliberate mistakes. Start the app (see README), open <http://127.0.0.1:8765>, and choose
**Open sample project**.

The header shows `rev-2 · saved` and the open violations; click it (or the **Issues** tab) to
list them. Each issue is the validator's own message, e.g. "CT-201: s223: Inconsistent
dimensionalities among the `Property`'s `Unit` and `Property`'s `QuantityKind`", with the repair
engine's summary under it, e.g. "s223:hasConnectionPoint: have 0, need 1" for TK-301. Clicking an
issue inspects its object below without leaving the list; **show in table** jumps to its row.

## 1. Correct an incorrectly interpreted point

1. Points tab → click the **Unit** cell of `CT-201` (it says Milligram per Litre).
   The assistant shows **"Units for 1 point"**.
2. Type: *The unit is wrong, this analyzer reads uS/cm.* → **Propose change**.
3. The proposal shows `CT-201 · Unit · Milligram per Litre → Microsiemens per Centimetre` and
   "Model check: 5 → 4 · ✓ fixes: CT-201 …". Open **Technical detail** to see the one operation
   and the exact triples removed/added.
4. **Apply**. The header moves to the next revision; the Unit cell shows a purple dot (set or
   confirmed by a person).

## 2. Reassign selected points to different equipment

1. Click the **Equipment** cell of `FT-201`, then Ctrl/Cmd-click the Equipment cell of `FT-301`.
   The assistant shows **"Equipment assignments for 2 points"**.
2. Sort or filter the table: the selection stays on the same two points (it holds ids).
3. Type: *These flow meters are on the RO skid, not the high pressure pump. They belong to RO-1.*
4. The proposal lists both points `P-201 High Pressure Pump → RO-1 Reverse Osmosis Skid`, both
   inside the selection. **Apply**.

## 3. Correct a graph relationship

1. Graph tab. RO-1 has two pipes to TK-201 (drawn side by side): `L-05` permeate and `L-06`
   brine. Click the **L-06** edge → **"1 connection"**.
2. Type: *This is the concentrate line. It should go from RO-1 to TK-301 Concentrate Tank, not
   the permeate tank.*
3. The proposal changes `L-06 · Connected to · TK-201 → TK-301` and reports the TK-301 inlet
   issue as fixed. The model may also ask a question (e.g. about the brine medium); questions
   are shown but don't block applying. **Apply**. Nodes you didn't touch keep their positions.

## 4. Inspect the evidence behind a proposal

In any proposal, open **Evidence**: the selected model rows as the agent saw them, any source
observations, the BuildingMOTIF skill sections it read, and the vocabulary terms its operations
use. Click a change row to select that object and open it in the **Inspector** (types with IRIs,
RDF in Turtle, correction history). Points built from uploaded lists also cite their source
records.

## Build from uploaded records

For a new project, choose WaTr, ASHRAE 223P or Brick before uploading a point list. In
**Sources**, upload a CSV and confirm the detected row or column layout. The **Records** view
shows the confirmed source rows. Select **Build model from these records**, optionally add a
hint about site names, and start the run. Watch progress in the assistant panel.

The proposal summarizes how many records matched the naming pattern, each token's vocabulary
mapping, and equipment classes. Review unresolved mappings and validation issues, then apply
the proposal. The Points and Equipment tables show the resulting model; clicking **in model**
beside a record jumps to its point. Open **Individual changes** for a detailed list or use
**Undo** to return to the prior revision. Records left outside the model can be submitted with
**Build the rest** after correcting or clarifying their source convention.

Before applying any proposal, you can type a response in the Assistant box above it and choose
**Reply to proposal**. For example, *A2 is an AHU, not a VAV*. The assistant sees the pending
draft and proposes a revision; review its updated changes and questions before **Apply**.

## 5. Apply and undo

After any apply, the toast offers **Undo** (also the header button and Ctrl/Cmd+Z). Undo returns
the head to the previous revision — the table and graph revert — and **Redo** restores it. A new
edit after an undo clears the redo history.

## 6. Stale proposals

Ask for another change, and before applying it make a direct edit (double-click any cell, e.g.
rename `LT-101`). The pending proposal turns orange: *"The model is now at rev-N; this proposal
was made against rev-M"*, with **Refresh on latest** (re-runs the same request on the current model)
and **Discard**. Applying a stale proposal is refused by the server (HTTP 409).

## 7. Reopen and export

Reload the page or restart the server: the project reopens at the same revision with all
corrections, locks, history and layout. **Export Turtle** downloads exactly the displayed
revision (the response carries `X-Revision`); **Export point table** downloads a CSV of points
with measurement, unit, equipment and sensor type.

## Scripted check (no UI)

`backend/tests/test_project.py` and `backend/tests/test_api.py` cover the same guarantees
without a browser; `tests/test_agent.py::test_agent_live_reassigns_points` runs step 2 against a
real model when `WORKBENCH_TEST_PROVIDER` is set.
