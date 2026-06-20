# Privacy Policy

_Last updated: 2026-06-20_

This Privacy Policy describes how the Queue Bot ("the Bot", "we") handles
information when you add it to a Discord server or interact with it.

## What we collect

The Bot stores only what it needs to run queues:

| Data | Why | Where |
| --- | --- | --- |
| **Server (guild) ID** | Identify your server's configuration | PostgreSQL |
| **Channel & role IDs** | Know where to post panels and who is an admin | PostgreSQL |
| **User IDs** | Track who is in a queue or active session | PostgreSQL |
| **In-game name** (optional) | Shown to other players in a session, if your server enables it | PostgreSQL |
| **Session timestamps** | Enforce per-activity cooldowns and daily limits | PostgreSQL |

We do **not** collect message content, email addresses, real names, IP
addresses, or any payment information. We do not use analytics or advertising,
and we never sell data.

## How long we keep it

* Queue entries are removed when you leave a queue or when a session starts.
* Session and rate-limit records are pruned automatically (rate-limit history
  older than 24 hours is deleted on a recurring schedule).
* **When the Bot is removed from a server, all of that server's data — config,
  activities, queues and sessions — is deleted automatically.**

## Your choices

* Leave any queue at any time with the **Leave my queues** button.
* Ask a server admin to remove the Bot to delete that server's data.
* For data questions or deletion requests, contact the Bot operator (see the
  Bot's `/about` command or the repository README).

## Children

The Bot is not directed at children under 13 (or the minimum age in your
jurisdiction). Do not use it if you are under that age.

## Changes

We may update this policy; material changes will be noted by the "Last updated"
date above.
