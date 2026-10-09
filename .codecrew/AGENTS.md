# Agents

This repository is part of a CodeCrew project — coordination state lives in
GitHub issues and PRs, per the protocol at
https://github.com/radiusred/gh-codecrew/blob/main/SPEC.md.

- **Version check.** Finish it before any verb but two:
  `gh codecrew version` and `gh codecrew identity token`, which read no
  pointer and change nothing. Compare the protocol `gh codecrew version`
  prints with the `codecrew:` field of `.codecrew/config.yml`: the binary
  must implement the same major and a minor at least the pointer's — the
  CLI checks the major alone. In a spoke the floor is the hub's field,
  which may be ahead of the spoke's: read it with
  `gh api repos/<hub>/contents/.codecrew/config.yml -H "Accept: application/vnd.github.raw"`
  (`<hub>` is the spoke pointer's `hub:`) under whatever `gh` auth the
  session has; if that read is refused, mint your seat identity
  (`export GH_TOKEN=$(gh codecrew identity token <slug>)`, per your role
  contract) and retry it at once. If the binary falls short and you
  install the tools, upgrade (`gh extension upgrade codecrew`); otherwise
  raise it with `gh codecrew checkpoint` and stop. Never upgrade mid-task.
  Only once the check passes, go on.
- `.codecrew/config.yml` names the hub; the hub's `.codecrew/roles/`
  holds the role contracts. Then read the contract for the role you were
  dispatched as before doing anything else — `gh codecrew roles show <role>`
  prints it with this project's `.codecrew/roles/<role>.local.md` extension
  appended (blank until the project writes one; in a hub `init` scaffolds the
  file with a comment saying what belongs there).
- `gh codecrew status` shows where the project is; `gh codecrew help`
  lists the workflow verbs. Blocked gates refuse with
  `refused[CODE]: detail` — act on the code, don't work around it.
- Plans before commits, decisions recorded when made, and the verifier is
  never the doer. Reviews are model reviews: a clean-context session under
  the reviewer contract — even in pure solo, where its findings land as a
  PR comment before the operator confirms.
- **Contract drift.** `gh codecrew status` reports when a `.codecrew/roles/` contract
  is missing or differs from the one embedded in the installed CLI. One
  that is missing, or still an earlier release's text, is brought up to
  date by `gh codecrew roles sync`, delivered as a housekeeping PR (SPEC §4)
  with no task. One that differs from every release's text is this
  project's own fork, and local conventions are legitimate: the
  coordination layer compares (`gh codecrew roles diff <role>`, full upstream
  text via `gh codecrew roles show <role> --latest`), decides what to adopt,
  and reconciles it in a task with the decision recorded, moving the
  project's additions into the role's `.local.md` extension. Never overwrite
  a fork blindly. This file is CodeCrew's too, and `roles sync` brings it up
  to date the same way, only while it is a release's text; the project's
  own instructions belong in the root `AGENTS.md`.
- **Dispatch authorization.** If you are the operator's primary session —
  not dispatched as any specific role — then when a role is routed to a
  GitHub App and that role's action is needed (a review, a verdict),
  dispatching a clean-context sub-agent session as that App is authorized
  and expected; use the dispatch prompt in
  https://github.com/radiusred/gh-codecrew/blob/main/docs/identities.md. A
  session dispatched *as* a role never dispatches another role — that
  belongs to its coordination layer (platform, orchestrating session, or
  operator) — and never chooses or briefs its own judge.
