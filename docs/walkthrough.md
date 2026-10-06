# Walkthrough: build and correct a model

Uses the sample project (`samples/ro-train/model.ttl`): a brackish-water RO train with three
deliberate mistakes. Start the app (see README), open <http://127.0.0.1:8765>, and choose
**Open sample project**.

The header shows the current revision with its summary (`rev-2 …`; click it for the history)
and the open violations; click the violations (or the **Issues** tab) to list them. Each issue is the validator's own message, e.g. "CT-201: s223: Inconsistent
dimensionalities among the `Property`'s `Unit` and `Property`'s `QuantityKind`", with the repair
engine's summary under it, e.g. "s223:hasConnectionPoint: have 0, need 1" for TK-301. Clicking an
issue expands its full message, validation findings and available repair details directly in
the list, including while auto-fix is running. It also inspects the affected object below
when one is available; **show in table** jumps to its row.
Adding several issues to the assistant prompt, together or through separate **add to chat**
clicks, combines them into one readable summary grouped by validation rules, rather than
a JSON dump. Common paths and metadata appear once, and each repeated message lists its
issue-to-object mappings. Object IRIs appear once; when a finding's focus is that object's
IRI or its value equals its focus, the summary states that relationship instead of repeating
the IRI. Different focuses, values, shapes and severities remain explicit.
Repeated additions of the same issue are ignored, and your instructions outside that section
are preserved. Editing the generated section yourself keeps your edits; later additions
start a new section rather than rewriting them.

## 1. Correct an incorrectly interpreted point

1. Points tab → click the **Unit** cell of `CT-201` (it says Milligram per Litre).
   The assistant shows **"Units for 1 point"**.
2. Type: *The unit is wrong, this analyzer reads uS/cm.* → **Propose change**.
3. The proposal shows `CT-201 · Unit · Milligram per Litre → Microsiemens per Centimetre` and
   an **Addresses … issues** section and whether it introduces new issues. Open **Technical detail** to see the one operation
   and the exact triples removed/added.
4. **Apply**. The header moves to the next revision; the Unit cell shows a purple dot (set or
   confirmed by a person). The applied proposal folds into a summary; choose **show** to
   inspect it again.

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

For a new project, choose WaTr, ASHRAE 223P or Brick before uploading a point list. Open
**▸ Sources** at the left of the tabs (it starts closed until a project has sources), upload a CSV and confirm the detected row or column layout. The **Records** view
shows the confirmed source rows. Select **Build model from these records**, optionally add a
hint about site names, and start the run. Watch progress in the assistant panel.

The proposal summarizes how many records matched the naming pattern, each token's vocabulary
mapping, and equipment classes. Review unresolved mappings and validation issues, then apply
the proposal. The Points and Equipment tables show the resulting model; clicking **in model**
beside a record jumps to its point. Open **Individual changes** for a detailed list or use
**Undo** to return to the prior revision. Records left outside the model can be submitted with
**Build the rest** after correcting or clarifying their source convention.

Before applying any proposal, type a response in the Assistant box above it and send it as a
reply to the proposed change. For example, *A2 is an AHU, not a VAV*. The
assistant can inspect the pending draft, including newly proposed objects, and returns an
updated proposal; review the complete updated changes and questions before **Apply**.
Source builds preserve extraction evidence and do not lock fields when applied. Direct edits
and ordinary correction proposals do lock the fields you set or confirm.

For images, PDFs and documents, open the source and choose **Build model**. A PDF lets you
choose **Pages to read**, such as `1-3, 5`, with up to eight pages per build. You can build
additional pages afterwards. Use a model with image support for diagrams or pages without
a text layer. The source viewer lets you inspect pages and their extracted text.

Reconfirming a CSV layout keeps unchanged source records and their evidence links. Changed
records that have not been modeled are replaced; records already linked to the model remain
available as evidence.

## Continue a conversation

Answer the assistant's questions in the same chat. Questions and option labels are rendered
as readable text; choosing a checked auto-fix option is described below. A follow-up with no
new selection keeps the previous selection, dropping objects that no longer exist. To start
a separate request, choose **Clear chat** once the current run finishes. This clears the
visible conversation and draft from this browser; stored runs, proposals and model history
remain on the server.

The **Tokens** display counts the project's provider-reported sent and received tokens across
chat, source builds and auto-fix, including retries and failed replies. Clearing chat keeps
these totals. Providers that omit usage cannot be counted. You can change the model in the
Assistant panel; its status reports whether the configured endpoint is available.

## Auto-fix validation issues

In **Issues**, select the issues to address or run auto-fix for all open violations. Auto-fix
groups related issues and applies a fix automatically only when its checks pass: it resolves
the selected issues, introduces none, stays within the affected objects, deletes nothing,
and preserves fields confirmed by a person. New objects are allowed when needed, and the
chosen vocabulary terms must be supported by the model, issue or evidence.

When a decision is needed, the report offers checked option buttons. Choosing one applies
that option and dismisses the alternatives. **Something else… (chat)** lets you discuss a
different answer. Other groups may need review or more information; one failed group does
not stop the rest. **Undo automatic fixes** is available while those revisions are still
the newest. The results can be collapsed after review.

Tabs depend on the project vocabulary. WaTr includes **Processes** and **Media**;
Brick uses RealEstateCore spaces and has no Connection points tab. Use the Inspector's
**Relationships** panel for ontology relations beyond the typed fields. Proposal rows mark
changes outside your selection and overrides of earlier human edits.

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
corrections, locks, history and layout. **File ▸ Export Turtle** downloads exactly the displayed
revision (the response carries `X-Revision`); **File ▸ Export point table** downloads a CSV of points
with measurement, unit, equipment and sensor type.

## Scripted check (no UI)

`backend/tests/test_project.py` and `backend/tests/test_api.py` cover the same guarantees
without a browser; `tests/test_agent.py::test_agent_live_reassigns_points` runs step 2 against a
real model when `WORKBENCH_TEST_PROVIDER` is set.
