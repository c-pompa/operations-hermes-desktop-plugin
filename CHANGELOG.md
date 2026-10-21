# Changelog

The plugin's version lives in three places that must agree: `plugins/operations/plugin.yaml`,
`plugins/operations/dashboard/manifest.json` and the catalogue entry at
`docs/catalog/operations.yaml`. CI's `version-check` job fails when they disagree, or when the
version below has no section here, so `hermes plugins update` and the catalogue can always say
which build an install is on.

## 0.3.0

**Connect GitLab without leaving the page.** Merge requests, merged changes and a GitLab fleet
standard read nothing without a login, and the step only ever offered a `hermes config set` line to
copy. The setup checklist's **Connect GitLab** step now finishes in place:

- it names the logins this machine already holds and offers one press to adopt one: this profile's
  environment, another profile's `.env`, the `gitlab-mr` plugin's host, or the `glab` CLI. The page
  is told which logins exist and whether each has a token, never a token
- or it takes a host and token typed into the step, as a masked field
- either way `POST /setup/gitlab` writes them where Hermes keeps them: the host through the
  plugin-settings writer, the token through the `.env` credential route under
  `OPERATIONS_GITLAB_TOKEN` — never into `config.yaml`, never into this plugin's `plugin-data`, and
  never echoed back
- a source that holds only a token is offered for the host the plugin is already pointed at

**"Turn Operations on in every profile" became finishable.** It asked for something eight profiles
will never do (a worker or a chat-only profile has no use for the page), so it could never be
ticked. It now covers this profile — the one whose page you are looking at — and names the rest
without blocking on them.

## 0.2.0

Everything up to 0.1.0 was the first cut of the page; this is the pass that came out of running it
against a real fleet for a few weeks.

**Alerts that stop working now say so.** The Needs attention poll swallowed every failure: 1,911
`could not run in this profile's plugin host` answers in one night produced nothing on screen, so a
dead backend read as "nothing to report". Three failures in a row now raise one toast, the
Notifications tab keeps the time polling started failing and the last error, the poll backs off to
5, 10 and then 15 minutes while still retrying, and one toast says when it is checking again.
Polled queries keep one retry, not three.

**The setup checklist can see GitLab.** Merge requests, merged changes and a GitLab fleet standard
read nothing without a login, and the checklist never mentioned it: three pages sat half empty with
no hint why. `GET /setup` now reports the login (one `GET /user`, cached five minutes) and a
**Connect GitLab** step says which half is missing, or that the host did not answer.

**Handing over a card starts that card only.** It used to run a whole-board dispatcher tick, so a
click could start any other card sitting in ready, up to a minute before the gateway's own tick.
It now calls the dispatcher's own public one-card step, `kanban_db_dispatch.start_task_once`, which
takes the same board lock, spawn budget and per-profile cap and claims only that card; on a Hermes
that predates it the plugin falls back to the tick and names every other card it started. The
response's status is the card's status after the attempt.

**GitLab reads cost less.** GET answers are reused for 30 seconds, so a Merge requests page polling
a 40-MR window no longer asks for the same pipelines and job lists on every poll; a 429 with a
short `Retry-After` is waited out once and otherwise reported with its wait. `/changes` reads
merged MRs 100 at a time up to 500, and both MR routes say when the window holds more than they
show, instead of silently undercounting.

**The feature that is off is off at the API.** Role editing (`PUT`/`DELETE /roles`) and a rule
proposal aimed at an organisation answer 403, as the page's own gating already implied, and the
org proposal is refused before anything is read.

**Guards.** CI parses both JavaScript entry points with node (`js-syntax`), so a syntax error fails
the MR rather than blanking the tab on the machine that installed it.

## 0.1.0

First release: the Overview, Topology (with replay and the router's roles), Flow, Activity
(optional model-written summaries), Handoffs (with kanban card actions), Trace, Merge requests,
Conformance and Notifications pages, plus Desktop and Discord alerts.
