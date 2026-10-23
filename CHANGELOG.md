# Changelog

The plugin's version lives in three places that must agree: `plugins/operations/plugin.yaml`,
`plugins/operations/dashboard/manifest.json` and the catalogue entry at
`docs/catalog/operations.yaml`. CI's `version-check` job fails when they disagree, or when the
version below has no section here, so `hermes plugins update` and the catalogue can always say
which build an install is on.

## 0.3.4

**A failed model call now says what it is and what to do about it.** The Overview's Errors listed a
provider's own words and stopped there. An operator reading `APIStatusError: Error code: 402 -
{'error': {'message': 'Insufficient Balance …'}}` had to go to another machine to work out which
account was empty (the provider's platform, not the portal that was funded), what to change, and the
exact command to type.

Every group of failed calls the core's own classifier can name now carries a diagnosis under the
error text, in plain words, visually secondary to it:

- the reason class, from the app's single classifier (`agent.error_classifier`, by way of
  `cron.scheduler_failure_copy`) — the plugin holds no second ladder of status codes and substrings
- a cause that names the provider that refused, and for a billing refusal says plainly that it is
  THAT provider's account that is out of credit, not another account or portal the same person funds
- the concrete remedy with its commands, prefilled from the record: a cron job's id comes out of its
  run session (`cron_<job_id>_<timestamp>`), so a refused account reads
  `hermes cron edit <job_id> --provider <name>` then `hermes cron run <job_id>`, a rejected sign-in
  `hermes auth add <provider>`, a missing model `hermes cron edit <job_id> --model <name>`. A
  desktop or CLI failure gets the provider-level remedy. **Fix this** hands the same diagnosis to the
  chat or card it hands the failure to.

A failure whose text names no class keeps the error text alone, and a Hermes predating those core
modules serves exactly what it served before: every import is feature-detected.

## 0.3.3

**Connect GitLab no longer reads another tool's credential store.** The step offered every GitLab
login it could find on the machine, and two of those were files it parsed itself: another profile's
`.env`, and the `glab` CLI's own config. That is a rule-11 problem in the catalogue's admission
policy — a plugin gets secrets through Hermes' routes, not by reading credential stores — and it was
the one place this plugin reached into a store it does not own. The step now offers:

- this profile's environment, read through `get_secret` so the active profile decides which value is
  seen
- the host the `gitlab-mr` plugin is configured with — a host, never a token
- a host and token typed into the step, written through the plugin-settings writer and the `.env`
  credential route as `OPERATIONS_GITLAB_TOKEN`, never sent back to the page

A login that exists only in `glab` or in another profile's `.env` is typed in once instead of
adopted. Everything else about the step is unchanged: the probe, the two settings it writes, and the
rule that a token is never returned, logged, or put in an error message. The tests plant those files
and assert nothing in the setup payload comes from them.

## 0.3.2

**Nothing of the author's fleet ships to anyone else.** Every install was getting this machine's
environment in three places:

- the catalogue screenshots were shots of a real fleet — hostnames, profile names, local model
  names, role aliases, and the conformance standard's project, commit and dates. The two that
  carried them (Topology, Conformance) are gone until they are retaken from demo data; the listing
  keeps the Overview shot, which carries only numbers
- the published tests used real profile names and this GitLab's hostname as fixtures; they are now
  `alpha-eng`, `beta-cloud`, `gamma-lab`, `delta-harness` and `gitlab.example.com`
- a repair command told people to run the source-install venv binary with `-p default`: an install
  path that only exists on a source install, and a profile that may not exist. It reads
  `hermes gateway restart` now

**The rolectl-mcp advice is gated on having it.** The once-a-day "Operations can do more" toast
fired whenever no profile could fix router roles, so an install running a different router was told
to enable a helper it does not have — and the chat brief sent it hunting for the file. Both are
gated on `GET /models/agents` reporting the path; without it the brief says this router has no
rolectl instead of guessing.

`ci/no_personal_data.sh` (CI job `no-personal-data`) now fails the build on a home path, machine
name, tailnet address, our profile names, this GitLab's hostname or a source-install path in
anything under `plugins/` or `tests/`. AGENTS.md says the same, plus the rule CI cannot enforce:
screenshots come from demo data, or the view gets no screenshot.

## 0.3.1

**A page that is older than its plugin now says so.** A profile with its own copy under
`~/.hermes/profiles/<profile>/plugins/operations` is served that copy and never the shared
install, so a copy left behind serves an old page that looks exactly like a current one — which is
how 0.2.0 reached nobody on this machine for a day. `GET /setup` now reports both versions
(`copy: {page, shared, stale, newer, hint}`), and the Notifications tab carries an **old copy** row
naming them and the command that fixes it (`hermes -p <profile> plugins update operations`), shown
whether or not the setup checklist still has steps left. A copy that is *newer* than the shared
install says that too, rather than calling it stale.

Versions are compared as versions, not strings: 0.10.0 is newer than 0.9.0.

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
