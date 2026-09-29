# Privacy

Sortroom is software you run yourself. The Sortroom project runs no service, has no
servers of its own and receives no data from your installation – no telemetry, no
analytics, no update checks.

This page describes what an installation does with your mail. "You" is whoever runs
it; for the OAuth apps you create for Google or Microsoft, you are also the app's
developer and its only user.

## What Sortroom reads

Sortroom connects to the IMAP mailboxes you add. It reads new mail in the inbox
(headers and text) to sort it, and the folder lists and message headers of your
folders when it reconciles its log. It moves mail into folders and sets or removes
the flag (star). It never sends, forwards or deletes mail.

With a Google or Microsoft sign-in, Sortroom asks for IMAP access
(`https://mail.google.com/` at Google, `IMAP.AccessAsUser.All` at Microsoft) – the
only permissions that allow IMAP – and uses it for nothing else than the above.
Sortroom's use of information received from Google APIs adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## What goes to the classification model

For each new mail Sortroom sends the classification endpoint you configured the
sender, the recipient, the subject, the attachment names, whether it is a mailing list
and up to 3000 characters of the text (by default; quotes and signatures removed).

- With a **hosted** endpoint (e.g. OpenRouter), this data goes to that provider and to
  whoever runs the model behind it. Their privacy terms apply; check them.
- With a **self-hosted** endpoint, it stays in your network.

Nothing else leaves your installation, apart from the IMAP connection to your mail
server and, when you sign in with Google or Microsoft, the requests to their sign-in
service.

## What Sortroom stores

All of it stays on the machine that runs Sortroom, in its folders:

- `mailboxes/<id>/data/state.db` – a log of every processed mail: sender, subject,
  date, category, confidence, target folder, expiry date and cost; and a log of the
  runs. Mail text is not stored.
- `mailboxes/<id>/reports/` – dry-run reports with sender, subject and category.
- `logs/sortroom.log` – the application log; it can contain subjects (of mails moved by
  hand, and of every mail with verbose logging).
- `mailboxes/<id>/secrets.toml` and `config/secrets.toml` – the IMAP login or the OAuth
  sign-in token, and the API key of the endpoint; plain text, readable only by the
  user that runs Sortroom (mode `0600`).

The admin UI sets one cookie, for the login session, and loads no external resources.

## Removing access and data

- Revoke a Google sign-in at [myaccount.google.com/connections](https://myaccount.google.com/connections),
  a Microsoft one at [account.live.com/consent/Manage](https://account.live.com/consent/Manage)
  (personal accounts) or [myapplications.microsoft.com](https://myapplications.microsoft.com/)
  (work accounts).
- Delete a mailbox's folder under `mailboxes/` to remove its settings, login and log.

## Questions

Open an issue at [github.com/mhoenes/sortroom](https://github.com/mhoenes/sortroom/issues).
