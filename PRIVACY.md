# Privacy

Sortroom is software you run yourself. The Sortroom project runs no service, has no
servers of its own and receives no data from your installation – no telemetry, no
analytics, no update checks.

This page describes what an installation does with your mail. "You" is whoever runs
it; for the OAuth apps you create for Google or Microsoft, you are also the app's
developer and its only user.

## What Sortroom reads and changes

Sortroom connects to the IMAP mailboxes you add and reads the mail it sorts (headers
and text): new mail in the inbox, older inbox mail when you start a backfill, and mail
in other folders when you re-sort a folder or try a category description with **Test
with the model**. When it reconciles its log, it reads the folder lists and the headers
of the mail in your folders.

It moves mail into folders and sets or removes the flag (star). With **deletion rules**
(off until you set them up) it moves mail older than a rule allows into the mailbox's
trash folder; it never deletes mail itself, but once the trash is emptied – by you or by
your mail provider – that mail is gone. It never forwards mail.

With a Google or Microsoft sign-in, Sortroom asks for IMAP access
(`https://mail.google.com/` at Google, `IMAP.AccessAsUser.All` at Microsoft) – the
only permissions that allow IMAP – and uses it for nothing else than the above.
For Microsoft, Sortroom brings its own app registration so that you don't need one. That
gives the Sortroom project no access to anything: Microsoft sends the tokens of your
sign-in only to your installation, which keeps them in its `secrets.toml`.

Sortroom's use of information received from Google APIs adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## What goes to the classification model

For each mail it classifies – new mail, a backfill, a re-sort or a **Test with the
model** – Sortroom sends the classification endpoint you configured the sender (name
and address), the recipients, the date it was sent, the subject, the attachment names,
whether it is a mailing list and up to 3000 characters of the text (by default; quotes
and signatures removed). **Save & check** under Global settings sends a built-in sample
mail, none of yours.

- With a **hosted** endpoint (e.g. OpenRouter), this data goes to that provider and to
  whoever runs the model behind it. Their privacy terms apply; check them.
- With a **self-hosted** endpoint, it stays in your network.

## What else leaves your installation

Nothing, apart from:

- the IMAP connection to your mail server;
- when you sign in with Google or Microsoft, the requests to their sign-in service;
- the unsubscribe requests you send (below);
- the mail Sortroom sends itself, if you set it up (below).

## Unsubscribing

The *Senders* page lists senders whose mails carry an unsubscribe link (the
`List-Unsubscribe` header). When you press **Unsubscribe** and confirm, Sortroom sends
the sender's one-click unsubscribe request (RFC 8058): an HTTPS `POST` to the link from
the sender's mail, from the machine that runs Sortroom. The sender learns that the
address behind the link unsubscribed, and sees that machine's IP address. Sortroom
sends nothing else to senders, never on its own and never to addresses in your own
network. Links without one-click unsubscribe are only shown; you open them yourself.

## Mail Sortroom sends

Only if you set up an SMTP account under **Global settings → Mail** (off by default),
Sortroom sends mail through that server to the one recipient you enter there:

- **a notice when a scheduled task fails** – the mailbox's name, the task, when it
  failed and the error message (e.g. from the mail server or the model), and later a
  notice that it works again;
- **a daily summary** – per mailbox the sender and subject of the mails waiting to be
  reviewed (with the model's suggestion), of the mails starred in the last day and of
  offers that expire today or tomorrow, and the errors of the last day's runs;
- **a test mail** when you press **Save & send a test mail**.

With the address of the admin UI filled in, these mails contain links to it. Your SMTP
provider and the recipient's provider handle these mails like any other mail.

## What Sortroom stores

All of it stays on the machine that runs Sortroom, in its folders:

- `mailboxes/<id>/data/state.db` – a log of every processed mail: sender, subject,
  date, category, confidence, target folder, expiry date and cost; which of the model's
  decisions you corrected and to what; for undoing a run, where its mails came from and
  how their log entries looked before; the unsubscribe links of senders and when you
  unsubscribed; suggestions you dismissed; when tasks last ran and whether a failure was
  already reported; and a log of the runs. Mail text is not stored.
- `mailboxes/<id>/reports/` – dry-run reports with sender, subject and category.
- `mailboxes/.digest-sent` – the day the daily summary was last sent.
- `logs/sortroom.log` – the application log. It can contain senders and subjects: of
  mails moved by hand, of the mails a deletion rule names in its log, of every mail with
  verbose logging, and the subjects of the mails Sortroom sends.
- `mailboxes/<id>/secrets.toml` and `config/secrets.toml` – the IMAP login or the OAuth
  sign-in token, the API key of the endpoint and the SMTP password; plain text, readable
  only by the user that runs Sortroom (mode `0600`).

The admin UI sets one cookie, for the login session, and loads no external resources. Only the
API documentation (`/docs`, `/redoc`, after logging in) loads its viewer from `cdn.jsdelivr.net`.
The Mails page keeps the list's scroll position in the browser's session storage while
you open mails; it stays in that browser tab.

## Removing access and data

- Revoke a Google sign-in at [myaccount.google.com/connections](https://myaccount.google.com/connections),
  a Microsoft one at [account.live.com/consent/Manage](https://account.live.com/consent/Manage)
  (personal accounts) or [myapplications.microsoft.com](https://myapplications.microsoft.com/)
  (work accounts).
- **Delete mailbox** at the end of a mailbox's Settings removes its settings, login, log and
  reports from Sortroom for good (so does deleting its folder under `mailboxes/`), and
  revokes a Google sign-in.
- To stop the mail Sortroom sends, clear the SMTP server under **Global settings → Mail**
  or switch off the notices and the summary there. The SMTP password stays in
  `config/secrets.toml` until you remove its `[smtp]` section from that file.

## Questions

Open an issue at [github.com/mhoenes/sortroom](https://github.com/mhoenes/sortroom/issues).
