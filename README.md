# Sortroom

Sortroom sorts your IMAP inbox into folders. A classification model reads each new mail
and picks one of *your* categories – it doesn't generate text, so it can never invent a
folder. It runs as one Docker container with a web admin UI and works with any IMAP
server (Gmail, Outlook/Microsoft 365, your provider's mailbox, …).

> [!NOTE]
> **Sortroom is in beta.** Before version 1.0, settings, files and the HTTP API can still
> change in ways that need you to do something after an update. Such changes only come
> with a new minor version (e.g. 0.12 → 0.13) and are listed on the wiki page
> [Upgrading](https://github.com/mhoenes/sortroom/wiki/Upgrading) – read it before you
> update, or pin a version.

![Overview of a mailbox: last run, mails to review, received mail by folder, recent runs](docs/screenshots/overview.png)

## What it does

- **Sorts new mail** into the folder of its category – finance, purchases, travel,
  promotions, updates … Twelve standard categories come built in (English or German),
  and you can change them freely: the model files by the description you write.
- **Leaves uncertain mail in the inbox** and lists it under *To review*, where you
  accept the suggestion, pick another category or turn the sender into a rule.
- **Stars what needs action** – bills to pay, appointments to confirm, security alerts.
- **Tracks time-limited offers** and moves promotions to an *Expired* folder the day
  after their deadline, so they can be deleted in one go.
- **Sender rules** file known senders without asking the model (free).
- **Unsubscribe helper:** lists the senders with the most mail that carry an unsubscribe
  link, and unsubscribes in one click where the sender supports it.
- **Several mailboxes**, each with its own categories, schedule and log.
- **Runs by itself** every few minutes; mail you read stays read.

The model is reached through the [TypeSafe API](https://docs.typesafe.ai/api): any
service that speaks it works – a hosted one such as OpenRouter's decisions API (the
default), or a model you host yourself. With the default model on OpenRouter a mail
costs roughly $0.00002.

### Your mail and the model

For each new mail Sortroom sends the model the sender, recipient, subject, attachment
names, whether it is a mailing list and up to 3000 characters of the cleaned text.
**With a hosted endpoint this data goes to that provider** and whoever runs the model
behind it – check their privacy terms. With a self-hosted endpoint it stays in your
network. Apart from that Sortroom only talks to your IMAP server – and to a sender's
server when you unsubscribe from it in one click. Details: [PRIVACY.md](PRIVACY.md).

## What you need

- A machine that runs Docker (amd64 or arm64, e.g. a home server, NAS or Raspberry Pi).
- Access to each mailbox:
  - **Outlook.com / Microsoft 365** – the sign-in with your Microsoft account; nothing to
    set up ([details](https://github.com/mhoenes/sortroom/wiki/Outlook-and-Microsoft-365)).
  - **Gmail** – the sign-in through your own, free OAuth app, a few minutes to create
    ([details](https://github.com/mhoenes/sortroom/wiki/Gmail)), or an app password.
  - **Any other provider** – the IMAP user and password.
- An API key for the classification endpoint, e.g. from
  [OpenRouter](https://openrouter.ai/settings/keys) – or your own TypeSafe-API server.

## Quick start (Docker)

```sh
mkdir -p sortroom/config sortroom/logs sortroom/mailboxes && cd sortroom
curl -fsSLO https://raw.githubusercontent.com/mhoenes/sortroom/main/docker-compose.yml
curl -fsSL -o .env https://raw.githubusercontent.com/mhoenes/sortroom/main/.env.example
curl -fsSL -o config/config.toml https://raw.githubusercontent.com/mhoenes/sortroom/main/config/config.toml
sudo chown -R 1000:1000 config logs mailboxes
```

1. In `.env` set `ADMIN_PASSWORD` (at least 8 characters); in `docker-compose.yml` set `TZ`
   to your time zone.
2. `docker compose up -d`, open `http://<docker-host>:8765` and log in.
3. Under **Global settings** enter the API key and press **Save & check**.
4. Under **Add mailbox** choose Gmail, Outlook.com / Microsoft 365 or another IMAP server,
   enter the login and pick the standard categories.

From then on the mailbox is sorted every 10 minutes; new mail waits 24 hours in the inbox
first. The wiki page [Installation](https://github.com/mhoenes/sortroom/wiki/Installation)
has the details: securing the admin UI, updating, backup.

## Documentation

The [wiki](https://github.com/mhoenes/sortroom/wiki) has the full documentation:

- [Installation](https://github.com/mhoenes/sortroom/wiki/Installation) ·
  [Gmail](https://github.com/mhoenes/sortroom/wiki/Gmail) ·
  [Outlook and Microsoft 365](https://github.com/mhoenes/sortroom/wiki/Outlook-and-Microsoft-365) ·
  [Other providers](https://github.com/mhoenes/sortroom/wiki/Other-providers)
- [Admin UI](https://github.com/mhoenes/sortroom/wiki/Admin-UI) ·
  [Categories](https://github.com/mhoenes/sortroom/wiki/Categories) – how the model decides ·
  [Schedule and maintenance](https://github.com/mhoenes/sortroom/wiki/Schedule-and-maintenance) ·
  [Mailboxes and files](https://github.com/mhoenes/sortroom/wiki/Mailboxes-and-files)
- [HTTP API](https://github.com/mhoenes/sortroom/wiki/HTTP-API) (with n8n) ·
  [Command line](https://github.com/mhoenes/sortroom/wiki/Command-line) ·
  [Troubleshooting](https://github.com/mhoenes/sortroom/wiki/Troubleshooting) ·
  [Development](https://github.com/mhoenes/sortroom/wiki/Development) ·
  [Upgrading](https://github.com/mhoenes/sortroom/wiki/Upgrading)

## License

Copyright (C) 2026 Matthias Hönes

Sortroom is free software: you can redistribute it and/or modify it under the
terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. It is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
FITNESS FOR A PARTICULAR PURPOSE. See [LICENSE](LICENSE) for the full text.

Because the admin UI is a network service, the AGPL asks anyone who runs a
**modified** version for other people to offer those users its source code.
The sidebar links to this repository; if you change Sortroom and let others use
your instance, set `SOURCE_URL` in `.env` to your own source.
