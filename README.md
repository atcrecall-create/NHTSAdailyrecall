# Vehicle recall watch

A free, hosted dashboard of new NHTSA recalls that say "do not drive", mention fire,
or affect more than 50,000 vehicles. A GitHub Action checks NHTSA every day and
publishes the result to GitHub Pages. No server to run.

## Set up (about 10 minutes)

1. Create a new **public** GitHub repository (Pages is free for public repos, and
   recall data is public anyway).
2. Upload everything in this folder, including the hidden `.github` folder.
3. In the repo, go to **Settings > Pages**. Under "Build and deployment", choose
   **Deploy from a branch**, branch `main`, folder `/docs`. Save.
4. Go to **Actions**, open "Update recall dashboard", and click **Run workflow**.
   The first run loads the last 30 days of flagged recalls.
5. Your dashboard is at `https://YOUR-USERNAME.github.io/YOUR-REPO/`.

It then updates itself every day. Change the time in `.github/workflows/recalls.yml`.

## Daily email

In **Settings > Secrets and variables > Actions**, add three secrets:
`SMTP_USER` (your email address), `SMTP_PASS` (for Gmail, an App Password, not
your normal password) and `EMAIL_TO` (where to send it).

You'll get one email a day after the check runs. If new flagged recalls were found,
it lists them with badges, vehicle counts and NHTSA links. If not, it says nothing
new today. Either way it links to the dashboard.

To only get an email when something new is flagged, add this line under `env:` in
`.github/workflows/recalls.yml`:

```
EMAIL_ONLY_IF_NEW: "1"
```

Using something other than Gmail? Also set `SMTP_HOST` and `SMTP_PORT`.

## Run it on your own computer

```
python3 recall_watch.py --dry-run
```

Then open `docs/index.html`. `--dry-run` doesn't save state or send email.

## Adjusting

- Vehicle threshold: `VEHICLE_THRESHOLD` in `recall_watch.py`.
- History on the first run: set `BACKFILL_DAYS` (default 30).
- If fields look blank or shifted after the first run, NHTSA changed its file layout.
  Update the `COLS` list in `recall_watch.py`.

## Privacy

The dashboard is a public web page. Don't put anything private in this repo.
