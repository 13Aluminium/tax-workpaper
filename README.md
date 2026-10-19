# Tax Workpaper Prototype, core demo (Okafor-Lindqvist, 2025)

A web-native workpaper: every number on the return can be traced to its source,
checked, adjusted (without losing the original), signed off and filed.

This is the **core demo** version. It keeps only what is needed to show the
mistakes being found and fixed by three people:

- **5 tabs:** Overview, Issues & checks, Return lines, Documents, Filing
- **3 roles:** preparer (does the work), reviewer (checks it), partner (approves big fixes, files)

Left out compared with the full version: comparing engine versions, what-ifs,
judgment notes, review notes and client questions, the client portal, firm
settings and time budgets, extensions, amendments, IRS letters, roll-forward,
CPA reports, the timeline and file attachments.

The design is deliberately plain. The focus is on the behavior working end to end.

## Run it locally (about 2 minutes)

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000. Use the **Acting as** dropdown (top right) to switch
between Priya (preparer), Daniel (reviewer), Helen (partner) and Marcus (preparer).

To start over, stop the server and delete `workpaper.db`.

## A 2-minute demo path

1. **Issues & checks** as Priya: find "D23 page 3 is a second scan", click the blue `D23` link
   to see the duplicate page, then **Apply proposed adjustment(s)**. It is +$919, so it is active at once.
2. Apply "Home property tax includes 6,912". It is +$1,462, so the card says **waiting for partner approval**.
3. Switch to Daniel: he cannot approve it. Switch to Helen: she sees **Approve adjustment**.
4. **Return lines**: click `SchC.9` to see the engine value next to the workpaper value,
   how it was built, and where it came from.
5. **Filing**: the blockers list says exactly what is left before the partner can e-file.

## Run the tests

```bash
python tests/test_workflow.py
```

The test plays a whole season through the API. It covers finding the mistakes, a
partner approving big adjustments, sign-offs that reopen when a number changes, the
rule that you can't review your own work, bulk sign-off with undo, masking, filing
and freezing.

## Deploy to a link (Render, free tier)

1. Push this folder to a GitHub repository.
2. On https://render.com choose **New > Web Service** and pick the repository.
3. Use these settings:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn -w 2 -b 0.0.0.0:$PORT app:app`
4. Deploy, then share the `https://<name>.onrender.com` link.

Note: on Render's free tier the disk is temporary, so `workpaper.db` resets when the
service restarts or redeploys. That is fine for a demo. For durable saving, attach a
Render Disk and set `DB_PATH=/var/data/workpaper.db`.

## Project layout

| File | What it does |
| --- | --- |
| `app.py` | Flask web server: the page and the `/api/...` routes, permission checks |
| `packet.py` | Loads the JSON packet (read-only) and builds lookup indexes, including reverse links |
| `database.py` | SQLite schema (6 tables) and helpers. Everything humans do is stored here |
| `workpaper.py` | Core logic: effective values, sign-off fingerprints, tax-effect estimates, filing gate |
| `checks.py` | Independent checks that do not trust the engine (the "confident mistakes") |
| `permissions.py` | The 3 roles, allowed actions, masking of SSNs and account numbers |
| `static/app.js` | All browser code (plain JavaScript) |
| `templates/index.html`, `static/style.css` | The single page and its styles |
| `data/packet.json` | The packet exactly as delivered |
| `tests/test_workflow.py` | End-to-end season test |
