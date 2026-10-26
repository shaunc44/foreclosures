# Bergen County foreclosure watch

Weekly email of Bergen County sheriff's foreclosure sales: what's **new**, what **changed**
(sale date moved / status changed), and what **dropped off** (sold, cancelled or settled).
The source is the sheriff's free listing on salesweb.civilview.com, so you don't need a paid app.

## Setup (Mac)

```bash
cd bergen-foreclosures
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.json config.json      # edit: email, towns/zips (empty = whole county)
export FORECLOSURE_SMTP_PASSWORD='xxxx xxxx xxxx xxxx'   # Gmail app password
.venv/bin/python foreclosure_watch.py --no-email          # first run: everything is "new"
```

Gmail app password: Google Account > Security > 2-Step Verification > App passwords.

## Filter to your area

In `config.json`, set `"towns": ["Teaneck", "Hackensack"]` and/or `"zips": ["07666"]`.
A property matches if either one matches.

## Run it weekly

```bash
# edit /PATH/TO and the password in the plist first
cp com.user.foreclosurewatch.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.user.foreclosurewatch.plist
launchctl start com.user.foreclosurewatch     # test run now; check run.log
```

If the Mac is asleep at 7 AM Monday, launchd runs the job when it wakes. For a daily run,
delete the `Weekday` key.

## Output

- Email with HTML tables, plus the full current list attached as a CSV
- `reports/` holds a CSV and an HTML digest for every run
- `state.json` remembers what was seen. Delete it, or pass `--reset`, to start over.

## Flags

| flag | what it does |
|---|---|
| `--details` | opens each new/changed property's page for the judgment amount and current status (adjourned, etc.) |
| `--no-email` | prints to the terminal only |
| `--html-file page.html` | parses a saved copy of the list page, in case the site blocks scripted requests |
| `--reset` | ignores saved history |

## Notes

- Sales get adjourned constantly, so a sale date "changing" is normal. Confirm status before the sale.
- Sheriff's sales are subject to prior liens and taxes. Get a title search before bidding.
- This covers sheriff's sales only. For pre-foreclosures (newly filed lis pendens), search the
  Bergen County Clerk's free land records site by document type.
