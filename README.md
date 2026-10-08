# Operations

Hermes plugin: an Operations overview. It shows vitals for the chosen window,
today's hourly strip, what needs attention (including kanban cards waiting on a person:
blocked or in triage, with the worker's reason and a link to the card in Handoffs), changes and news (merged MRs, model loads),
hosts with the model pool, and recent activity. In the Today strip, hovering a mark (an hour's
requests, errors or tasks, an incident, a change) says what it is, and clicking it opens a side
drawer with its details and links: the map at that time, Flow, Trace, Handoffs, or the merge request. An hour with
errors lists them grouped by what failed and where (the error text, the entry point and host, profile, model and what
served it), each with links to its newest failing session in Trace, its model in Flow over that hour, and the
map over that hour with its profile selected. The map counts a client's failed calls as Flow does and draws
each line's failed share in red along it. An incident selects its host or gateway on the map, and a model change its device. An activity entry opens in a side drawer with
its raw lines, and a host with its stats, models and recent activity there. Links on every tab
open the same thing on another tab with it selected: an entry's card in Handoffs, a host or
device on the Topology map, its traffic in Flow (a device, a profile or an entry point), a
subagent session in Trace and back. The page opens on a time range that covers it: the range of
the page the link came from, or for a dated thing (an Activity entry, a Trace call, a handoff) the shortest range
that reaches back to it. A Flow tab charts the window's requests from
entry point to profile to model to what served them (a pool device, or the cloud provider
when no device did), with requests per hour by what served them. Each device or provider keeps
the color it got when this Desktop first saw it (none is red, which marks failures). Failed calls a client recorded count as requests, shown
as a red share of each bar (the router's own errors carry no entry point and stay in the Errors card); selecting any bar shows
where its requests came from and went. While one is selected (or opened from a "See its traffic" link) a bar above the chart
names it with its share of the window; each band shows its traffic in the color of what served it over the rest dimmed, the hourly chart
shows only its traffic, scaled to its busiest hour, failed counts are red, and
each other bar counts its requests through it. A link to something with no requests in the window says so and offers
the last 7 days; an error in Activity links to its model's traffic. An Activity tab lists errors, gateway state changes,
kanban task events, host check changes, merged MRs and model loads, grouped by hour and
filtered by kind; an entry opens to its raw lines. A Handoffs tab shows who handed work to
whom, as one lane per agent (a dot on the sender's lane, the bar on the receiver's: a card's runs
on a thin line from filing to result, hatched while open), a summary by kind and pair, and a
list of every handoff with its status. A selected handoff opens in a side drawer: a card's own
kanban events with their raw lines, or the trace of the session that started the subagents. Kanban cards (who created them, who was assigned, every run and
its outcome) and subagent delegations (grouped by parent session, with each subagent's model
and request count). An open card can be moved forward from there: hand it to a profile
(that profile's own worker starts on a ready card at once, with the card's history, comments
and earlier attempts, under the same kanban limits from `config.yaml` the gateway applies;
the page says when it could not start yet and why), or schedule it for a set time, mark it
done with a result, or close it as not needed (archive). A blocked card shows what its worker
is waiting for; reply and unblock it there, and the reply goes on the card (as "dashboard")
for the worker, which starts again at once. A scheduled card waits in kanban's
own "scheduled" status with its start time in the note; the plugin's Python half
(`__init__.py`) hooks the gateway's kanban dispatcher and, on the first tick after that time,
puts the card back to ready, so the profile's worker starts within about two minutes of it.
These go through Hermes' own kanban functions. A Trace
tab lists the window's sessions that made model calls; one opens to its calls in order on a
timeline, each with its turn, model, what served it (pool device or cloud provider), time to
first token, tokens and any failure, plus the subagents it started and, for a subagent, the
session that started it. "Why it took this long" splits the session's time into model calls
and the waits between them, names the longest wait, and lists calls that took at least twice
as long as usual. A selected call shows its time against that usual time, on one scale: the
median of calls like it as of when it ran (the same model on the same server, with half to twice
its output tokens, from other sessions, in the week before it started), with how many there
were. With fewer than five such calls it says so instead, so the baseline fills in and moves as
history builds. A Topology tab
maps each host's gateways (with their state), the hosts (live or stale, with load and memory),
and what served each host's requests in the window (pool devices with their loaded models,
and cloud providers), line width by request count. A host whose newest conformance report
fails a check is outlined as drifted (installs without host checks show none). Selecting a
node shows its details and what stops if it goes down: the entry points and requests cut off,
and the models that were also served somewhere else in the window. Where the model router
(hermes-model-router) answers, a Roles list shows each role alias's candidates in order, which
are loaded, and the pool device serving it now, and the blast radius adds what each affected
role does by the router's own rules: it moves to its first candidate still loaded and fitting
somewhere up, or, with none, a strict role stops and any other falls through to its last
candidate. The router is found the way its own plugin finds it (`MODEL_ROUTER_URL`, else its
default address, with `MODEL_ROUTER_TOKEN` / `ROUTER_TOKEN` or
`<hermes root>/model-router/router.token`); without one the page says so and keeps to observed
traffic. A track under the map replays it: drag to any moment in the window, or pick one of
the newest changes (a gateway, host or device changing state, a model loading or unloading, a
host starting to drift), and the map, states and line widths show that moment with the changed
node selected; a drifted host links to its failing check on the Conformance tab. The map has
six layouts: columns, segmented radial, arc diagram and blast radius draw hosts and what served
them; area grouping (a circle per host holding its profiles, the router, the model pool and the
cloud, circle area by token volume) and router switchboard (columns of entry points, profiles
by host with their harness, role aliases inside one router box, then pool devices and cloud
providers with the models each holds) follow each request from its entry point through its
profile and role alias to what served it. Their legend lists only the kinds of line drawn, an
idle router says "no requests", and hovering a node or line shows its request count. In area grouping the router has one line
to each device, or with "Show each role" a ring of its role aliases, the columns moving right to fit; when that line carries roles on fallback and roles on their first choice it draws
as fallback over the first-choice color, and its hover names both. On those two, "Collapse to one orchestrator" folds
every profile into one node. "Traffic" adds moving dots and a glow on what had requests in
the last two buckets (off by default when the system asks for reduced motion). A bar over the
map lists the active issues (a gateway down, a pool device offline, a host that stopped
reporting, roles on a fallback model, one issue per device they fell back to, a role nothing in the pool serves, which still gets its box, a drifted host), outlined in the worst one's color; an
issue opens a side drawer with why it is happening, the facts behind it, a command to copy where one fixes it,
and links to the tab that shows more (no traffic link when the issue has no requests), and
selects its node. "Issues only" keeps just the nodes
an issue points at and their neighbours. "Replay a day" plays the last 24 hours from the start,
a bucket every quarter second, and returns to live at the end. Keys: Space plays or pauses,
L goes back to live, I toggles issues only, and the arrow keys step to the previous or next
change or issue start. Under the map, what changed in the window shows as chips, with a repeated
change to one node, like a gateway restarted four times, as one chip with its count and time span.
A gateway restart that takes several platforms on a host down and back at once counts as one
restart, listing the platforms. When one server took most of the window's requests the line says
which profiles sent them and links to its traffic. Lines with no requests in the window are drawn
faint. A tick on the as-of track shows the change on hover and replays to it on click. In the Roles
table a role's name selects it on the map, and "See its traffic" opens it on Flow. "Compare all fifteen" shows the layout styles considered side by side
on the same data with what each is good for, and sets which layout each role opens with. A Merge requests
tab lists open MRs and those merged in the window: who each waits on (a reviewer, or the
author when the pipeline failed or there are conflicts) and for how long, its newest
pipeline's stages (a retried job counts by its latest run, an allowed failure as passed), and
counts by repo; one opens to its details with links to GitLab. A Conformance tab checks every project Hermes knows (the projects and repos in
Desktop's sidebar for the profile Desktop is on: the one you are working in, then the projects
you created, then repos found from sessions) against a built-in standard: no private keys, API tokens or merge conflict markers in
tracked files, and no hard-coded home directories in code. It reads the files git tracks in
each folder on this machine, up to 30 projects, and reads a folder again only once git shows
it changed (a commit, a checkout or an edited file), so a fix shows within a minute; a cell opens to
the lines found and the fix. With nothing selected, the side panel counts each check's failing
projects and hosts and names the hosts that drifted or stopped reporting; a count opens to the
list of what fails that check, and each entry to its evidence. A project with findings can be
handed to a profile from its cell: after a confirm step it files one kanban card (as
"dashboard") naming each finding by file and line with the fix, the project checked again
first; the lines themselves stay off the card, since for a key or token rule they are the
secret. Asking again while that card is open returns the same card. This needs no setup. A project can add its own checks with a
`conformance.yaml` at its root, in the same format as a fleet standard; its `absent` repo rules
(a pattern that must not appear in the files a glob matches) apply to that project only:

```yaml
repo_rules:
  - id: no-todo-markers
    title: No TODO markers in scripts
    severity: warns            # or blocks
    check: { kind: absent, pattern: 'TODO\b', paths: ["*.sh"] }
    exceptions:
      - { path: scripts/legacy.sh }
    fix: Finish it or file a card.
```

A rule whose id the built-in or fleet standard already defines keeps that definition, so a
repo cannot loosen them; a rule that does not parse or compile is named on the project's row
and the others still run. With a fleet standard, a **Conformance project** set (below) or a
local one, its "absent" rules join the built-in ones for every project, and the tab also
shows each host's checks against the same standard and, for a GitLab repo, the standard's
checks of that repo from its CI. No GitLab is needed: "Start a local standard" on the
Conformance tab writes a starter `~/.hermes/conformance/conformance.yaml`, used whenever the
setting is empty, and the setting also takes the path of any local `conformance.yaml`. A host with failing checks can be fixed from there in one of two
ways, and the dashboard itself never reaches into other hosts. Hand it to a profile: after a
confirm step it files one kanban card (as "dashboard") with the failing checks, their
evidence, why they matter, the fix and any command to run, and that profile's worker starts
on it; asking again while that card is open returns the same card. Or "I'll run it": the
confirm step lists each failing check with its fix and, where the rule gives one in an
optional `remediate` field, the command to run on the host, which the page shows to copy.
Either way the request is recorded and shows in Activity, and the host's panel says whether
it worked from the host's next report: waiting, fixed, or still failing (naming which). "Propose a rule" adds a host or repo rule to the standard as a merge
request on the Conformance project (a branch from the main it read, so a change merged since
shows as a conflict rather than being undone); the repo's own CI checks the rule there and a
person merges it. Main is never written directly. A local standard has no repo to review the
rule, so it is written into the file. Its check kinds and severities are the
ones the standard already uses.

**Business mode** is detected, never installed. When the Hermes account belongs to a shared
organisation and Hermes skill sync is on (`sync.enabled`), the Conformance tab names the
organisation and your role, and "Propose a rule" can send the rule to the organisation instead
of opening a merge request. The organisation's copy of the standard is an org skill named after
the standard's `standard:` name: its `conformance.yaml` gets the rule and its `SKILL.md` (the
rules in prose for people, between markers, so text written around them is kept) is regenerated
from it. It is the org mirror's copy when there is one, edited in place the way Hermes edits org
skills, else a new skill under `<profile>/skills/standards/<name>/` seeded from the repo's
standard. The rule then goes through Hermes' own sync client (`hermes sync propose`). Every role
sends the same request and the sync server decides: an owner or admin publishes it, a member's
waits as a proposal for an admin. The button's label guesses from your role, and the page shows
what the server answered. If the send fails, the edit stays in the local copy, and
`hermes sync propose <name>` sends it later. In an organisation with sync off, the tab says so.
Without an organisation, nothing changes. Proposals are reviewed outside the plugin for now,
because Hermes has no client call yet to list or decide them.

The page can be viewed as a role: Everyone, SRE, DevOps, AI and MLOps, or Cloud and platform.
Each role is a job description (a summary, its responsibilities and the skills it brings) plus
what it means for the page: the Activity kinds it watches first and the layout Topology opens
with. "Edit roles" changes any of them, adds or deletes roles, and puts one or all back to the
built-in descriptions; edits are saved for everyone on this Hermes install, and the role each
viewer picked is remembered in their own Desktop.

With the **Activity summaries** setting on (off by default), each Activity entry reads as one
plain sentence written by a small model from the entry's raw events. Every sentence says which
model wrote it (and its provider) and from how many events, and the entry still opens to the
logged text and raw lines. The model is whichever is set for the "Operations activity
summaries" auxiliary task in Hermes' model settings; the plugin calls no model while the
setting is off. Summaries are cached, so each entry is written once.

The plugin writes only through these actions: kanban cards (Handoffs and the Conformance fix
cards, through Hermes' own kanban functions), merge requests proposing a rule (never main),
its own files under `<hermes root>/plugin-data/operations/`: `roles.json` (saved roles),
`summaries.json` (the summary cache) and `fixes.json` (each host's newest fix request), and in
business mode the standard's org skill when a rule is sent to the organisation.

This is a fresh plugin, separate from Ops Timeline (`gitlab-mr`), which it leaves alone.
The plans and demo in the ops-timeline repo (`docs/FEATURES.md`,
`docs/prototype/ops-overview.html`) are the reference for later pages.

The plugin is one Hermes package, `plugins/operations/`:

| Path | What it is |
| --- | --- |
| `plugin.yaml` | manifest and the settings form |
| `__init__.py` | the kanban dispatch-tick hook that starts scheduled cards, loaded by the gateway |
| `dashboard/` | backend routes, mounted at `/api/plugins/operations/` by the Hermes dashboard |
| `desktop/plugin.js` | the page; Hermes Desktop copies it into `desktop-plugins/operations/` itself |

Data comes from the metrics collector's `<hermes root>/metrics/metrics.db`
([hermes-metrics-dash](https://github.com/c-pompa/hermes-metrics-dash)) and the kanban
board, both opened read-only. Changes and news also reads merged MRs from GitLab, and hosts
are paired with pool devices through Ops Timeline's
`<hermes root>/plugin-data/gitlab-mr/device-aliases.json` (read only). The web dashboard
tab is hidden; the page lives in Hermes Desktop.

While Hermes Desktop runs, Operations checks Needs attention every minute and alerts on
each item that newly appears (a gateway that dropped, a health finding, a host that went
quiet): an in-app toast with a link to the page, and a system notification while you are away
from Hermes, which Desktop's Settings > Notifications > "Plugin notifications" switches. More
than three at once arrive as one summary. Kanban cards are left to the Kanban plugin's own
notifications. What it has already alerted on is remembered across restarts; on the first run
it only records what is already listed, and an item that clears alerts again if it returns.

Hermes Desktop has no notification center, so each alert is also kept on the Notifications tab
(the newest 200, in this Desktop's plugin storage), with a count of the unread ones on the tab. An
alert stays unread until you mark it read, one at a time or with Mark all read.
The first time the tab's log is written (a new install, or an update from before it), the items
already open under Needs attention are listed too, tagged "already open", without a toast.

To get the same alerts with Desktop closed, give the gateway a place to post them:
`hermes config set plugins.entries.operations.settings.alert_target discord` (the Discord home
channel; `discord:<channel>` for another, or any other `hermes send` target), then restart the
gateway. It checks on the kanban dispatcher tick, about once a minute, under the same rules:
first run records only, kanban is left out, more than three arrive as one message. Set it in one
profile only, or each profile that has it posts its own copy.

On a fresh install Desktop shows a toast once saying how many setup steps are left, and the
Notifications tab opens with a "Set up Operations" checklist until they are done (or hidden),
with a small "N setup steps left" link next to the range on Overview that goes there: the
profiles that do not list operations under `plugins.enabled` yet (each needs it, then a Desktop
restart; a profile that lists it under `plugins.disabled` is left alone), the metrics collector,
the kanban board, the Conformance project, what happens when a host drifts, and, optionally,
Discord alerts. Each step says where to do it, and settings steps open the plugin settings.
Desktop builds that settings form only for a profile with its own copy of the plugin (under
`<profile>/plugins`), so for a profile that uses the copy in `~/.hermes/plugins` the step gives
the `hermes -p <profile> config set plugins.entries.operations.settings.<key> <value>` command
instead, with a Copy command button.

Settings set in the default profile apply to every profile that leaves them unset (or empty), so
the Conformance project, GitLab host, Activity summaries and what happens when a host drifts can
be set once on default's settings page; a checklist step says "Set in the default profile" for a
value it takes from there. This covers what the Operations page reads. The gateway still reads
each profile's own settings for Discord alerts and automatic fix cards, so those are posted and
filed once, by the profile that sets them.

"When a host drifts" (`drift_action`) picks what the Conformance tab does with a host whose
newest report fails a check: `button` (the default) offers "File a fix card" and "I'll run it";
`automatic` has the gateway file and start the same card for `drift_profile` on its dispatcher
tick, at most once a day per host; `off` shows the failing checks only. Without a kanban board
only "I'll run it" is offered.

A missing source shows as "unavailable" or "not set up" on its own section. If the metrics
database or the kanban board does not exist yet (a fresh install), the page still loads:
that source reads as empty, a note at the top says which one is missing, and the other
source fills in as usual. Nothing is created.

## Install

```sh
hermes plugins install "https://github.com/c-pompa/operations-hermes-desktop-plugin.git#plugins/operations" --enable
```

Or in Hermes Desktop: Capabilities > Plugins > Install from Git, with the same URL. Then
switch Operations on in Capabilities > Plugins (Hermes installs a package's desktop page
off, and remembers the choice) and restart the dashboard so it mounts the backend. `hermes plugins update operations` pulls
new versions.

## Settings

All optional, in Capabilities > Plugins > Operations (the gear), stored under
`plugins.entries.operations.settings` in the active profile's `config.yaml`. Settings are
per profile, so set them in the profile the desktop app is using
(`hermes -p <profile> config set plugins.entries.operations.settings.<key> <value>`):

- **Conformance project** (`group/repo`, or the path of a local `conformance.yaml`) adds a
  fleet standard to the Conformance tab: its `absent` repo rules are also checked in each
  project, and the fleet checks show. Empty, it is `~/.hermes/conformance/conformance.yaml`
  when that exists (the tab's "Start a local standard" makes it). A GitLab repo's CI job
  `standard` must publish `conformance-report.json`; its `conformance.yaml` supplies rule
  titles and fixes, and the hosts and host rules for the host grid. Host results come from
  the `conformance_results` table in `metrics.db`, written by the metrics forwarder on
  each host once its `CONFORMANCE_STANDARD` points at a copy of that `conformance.yaml`
  ([hermes-metrics-dash](https://github.com/c-pompa/hermes-metrics-dash) `host_checks.py`). A host with no report in 25 minutes shows as
  stale, a declared host that never reported says so, and a result whose sender's ingest
  token could not vouch for the host is marked unverified.
- **Activity summaries** (on or off, default off) rewrites Activity entries as sentences with
  the model set for the "Operations activity summaries" auxiliary task (see above).
- **GitLab host** and **GitLab token** override your `glab auth login` (its default host
  and that host's token). `GITLAB_HOST` / `GITLAB_TOKEN` in the environment work too.
  Reading needs `read_api`; proposing a rule opens a merge request, which needs `api`.

## Test

```sh
~/.hermes/hermes-agent/.venv/bin/python3 -m unittest discover -s tests
```

## Later ideas

- **Shared plugins in Hermes itself.** Operations is installed once, in `~/.hermes/plugins`, but
  every profile has to list it under `plugins.enabled` before Desktop shows it, and
  `hermes -p <profile> plugins enable operations` fails for named profiles because it only looks
  in that profile's own plugins folder. A change in hermes-agent could fix both: treat a plugin
  installed in the root folder as available to every profile (still honouring each profile's
  `plugins.disabled`), or let the enable command find root-installed plugins. That would let the
  setup checklist drop its "turn on in every profile" step. It needs an upstream change, and
  hermes-agent keeps profiles separate on purpose, so it may not be accepted. Managed scope
  (`/etc/hermes/config.yaml`) is not a substitute: it replaces each profile's whole
  `plugins.enabled` list rather than adding to it.
