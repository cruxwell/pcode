# Portable sessions and SSH handoff

Status: proposed implementation plan. No portable-session feature is implemented
by this document. Keep implementation choices small and verify the current code
before extending it; command names and storage schemas below are intentionally
not prescribed.

## Goal

Start a coding session locally, move it to a remote machine partway through a
long task, close the laptop, and later bring the session back. Preserve the
conversation, task plan, files, installed tools, and environment changes made by
the session. Continue from a safe checkpoint rather than restarting the task.

A portable environment should also restrict access to the local machine. Users
should not have to reinstall their project tools for every new conversation.

## Agreed scope

- Keep native execution available, with the existing optional Seatbelt/bubblewrap
  sandbox. Do not silently change native-mode behavior.
- Add an opt-in portable mode, remembered per project/directory. Portable sessions
  start inside a Linux container because the user may decide to offload later.
- Run the entire execution side inside that container: session host, agent,
  workers, shell jobs, file tools, and enabled execution integrations. A trusted
  host-side launcher manages containers and transfer. The terminal can initially
  run inside the container too; a network-transparent local UI is not a prerequisite.
- Start with Docker locally and Docker on an existing, user-supplied Linux VM
  reachable over SSH. Do not require a registry or managed sandbox service.
- Transfer a committed image with save/load over SSH, plus a separate workspace
  and session bundle. SCP of an archive or a streamed transfer are both acceptable.
- Support a complete local-to-remote-to-local round trip, including detached
  remote execution and reattachment.

Defer cloud provisioning, billing, Kubernetes/Docker-in-Docker, registry-backed
transfer, live process migration, cross-architecture conversion, and automatic
merging of divergent environments. Keep backend-specific operations contained,
but do not build a generic cloud framework before a second backend exists.

## Launcher behavior

In portable mode, the host `pcode` command selects and starts or reuses the project
container, then connects the terminal to the inner pcode process. Forward ordinary
session options, consume host-side lifecycle options, and translate paths where
needed. Make the inner execution path explicit so it cannot recursively launch
another container. Do not forward the entire host environment or grant the inner
agent access to the host's container-management credentials. Start with Docker;
Podman can be a later runtime adapter rather than a second v1 implementation.

## Environment lifecycle

Treat a conversation, an environment, and an environment checkpoint as distinct
things. A new conversation must not imply a clean environment.

Keep local state mapping a project/directory and environment profile to its
starting image, latest private checkpoint, platform, reusable container, and
mount/setup configuration. Handle Git worktrees deliberately: they can share an
environment baseline without sharing mutable session state. Machine-specific
container references belong in local state, not tracked project configuration.

Use a modest development base with pcode and common tools. Allow a project image
or Dockerfile to supply language versions and system dependencies. Do not require
users to perfect a Dockerfile before starting: installations performed inside the
container should persist. Record immutable image identities and architecture.

- Sequential conversations may reuse the retained project environment.
- Save private checkpoints at safe boundaries and before handoff. A commit is
  not necessary merely to retain a stopped container's installed packages.
- Concurrent sessions get independent writable environments and workspaces.
  Preserve both branches; do not let the last session to exit silently overwrite
  another session's environment default.
- Provide ways to inspect, reset, restore, and remove environments. Retain rollback
  points and prevent cleanup from deleting an active or in-transfer environment.
- Automatic private persistence is distinct from publishing a shared baseline.
  Reusable team setup should remain reproducible from reviewed project configuration.

Dependencies under mounts need an explicit policy. Container commits capture
changes in the container filesystem, not bind mounts or volumes. Required data
must be included separately or recreated; disposable caches can be omitted.
Avoid copying the user's host package stores into the portable environment.

## Handoff contract

A handoff moves execution ownership, not just files. Preserve the logical session
identity and its active conversation branch. Forking a conversation is a different
operation and must not accidentally become the implementation of migration.

### Reach a safe boundary

Introduce an intentional pause/drain operation. Stop before the next model request
once the preceding tool batch and its results are settled. A request to offload
need not wait for the current task-plan item or entire user turn to finish.

Account for background jobs and delegated workers, not just foreground tool
calls. For v1, wait for them to finish or require an explicit cancellation
choice. Do not automatically replay an interrupted command with uncertain effects.
Finish an in-flight model/tool exchange before pausing where practical. Show what
is delaying handoff and allow the user to abandon the handoff without losing work.

Freeze session-controlled writers before capturing state. A container commit's
process pause is not a substitute for this application-level boundary. Host
editors and other processes can still modify mounted files: isolate the workspace
or detect conflicting writes rather than silently producing a mixed snapshot.

### Capture one consistent checkpoint

The portable unit contains:

- The environment image, including filesystem changes made after startup.
- Workspace/Git state: commits, uncommitted changes, required untracked files, and
  enough repository metadata to recreate an independent checkout or worktree.
- Conversation and execution state: journal, step database, active branch, current
  request, task plan with stable IDs, and the continuation point.
- Queued instructions, pending user interactions, and relevant limits/usage state,
  handled without loss or duplicate delivery. Waiting for input remains waiting.
- Referenced artifacts, such as spilled tool results, attachments, and retained
  job/worker records needed to inspect or continue the session.
- A versioned manifest describing compatibility, image/platform identity, paths,
  mounts, and integrity checks. Credentials are provisioned separately.

Use a consistent database backup, not a live SQLite file copy. Do not assume a
worktree's `.git` pointer is portable. Recreate paths and runtime bindings without
rewriting historical model messages. Do not restore source PIDs, sockets, or
process-local handles as if they still represented live jobs.

### Transfer and activate

1. Source pauses and records a durable handoff attempt.
2. Transfer the image and bundle to a private staging location on the destination.
3. Destination verifies integrity, compatibility, credentials/integrations, and
   storage, then imports without allowing the agent to execute yet.
4. Commit the ownership change durably and prevent the old owner from starting
   more work. Only then may the destination continue the pending request.
5. Keep the source checkpoint until destination success is confirmed. The source
   becomes an observer, not another writable copy of the session.

Choose a small, explicit protocol with idempotent operations and durable attempt
and ownership identifiers. Before ownership transfer, failure can abort back to
the source after fencing the staged destination. After ownership transfer, a lost
acknowledgment must not cause automatic local resumption. Reconcile ownership
first; uncertainty should leave work paused rather than create two writers.

Recovery must also cover a source or destination restarting during handoff. Do
not promise exactly-once external effects after arbitrary crashes; record and
surface uncertain outcomes rather than blindly repeating them.

Returning locally uses the same protocol. Do not overwrite edits made locally
while the remote session was working. Restore into a separate workspace or stop
for explicit conflict resolution. Remote execution must outlive the SSH client
and remain discoverable for reattachment.

## Worker and transfer boundary

Keep a small trusted worker/controller capable of accepting and validating a
bundle, loading an image, starting/attaching a session, reporting status, and
checkpointing/exporting it. Keep these operations separate from Docker and SSH
mechanics so a future backend can reuse the handoff contract.

For v1, use authenticated SSH with normal host-key verification. Do not expose a
public upload endpoint or the Docker daemon API. Keep Docker access with the
trusted controller, outside the agent's environment. Transfer failures must leave
incomplete imports inactive and removable, and must not destroy the source.

Save/load sends image layers as an archive and is not an incremental transfer
protocol. Accept that initially; measure before adding a registry or custom layer
synchronization. Preserve image identity and verify the imported artifact.

## Credentials and authenticated profiles

Credentials have a separate lifecycle from environment images and ordinary
workspace/session bundles. Do not make an authenticated profile part of a shared
project baseline.

- For API keys, let the trusted launcher supply an explicit allowlist from configured
  sources, with authorization remembered per project/target where appropriate.
  Transfer authorized credentials separately over SSH and make them available at
  runtime, without embedding values in image/container configuration or command
  arguments. Never copy `.envrc`, shell profiles, or the whole host environment.
- Define whether a worker's credentials are temporary or retained privately outside
  snapshots for restart. Runtime injection alone does not prevent agent tools from
  reading credentials; document the exposure and constrain child environments.
- For Claude Code, use the official, unmodified CLI login inside the container.
  Retain the required profile state in private storage excluded from image commits.
  Investigate an explicitly authorized, opaque profile transfer between the user's
  own machines rather than extracting tokens or requiring login on every worker.
  Determine which files the pinned CLI needs; do not assume one directory contains
  every authentication mode's state. Avoid simultaneous mutation of a migrated
  profile by source and destination.
- Validate authentication before changing execution ownership. Test profile
  migration through refresh, process restart, and the return trip, not just one
  successful request. Offer official re-login when migration is unsupported or
  credentials have expired. Preserve the distinction between API-key and CLI-login
  providers rather than silently changing the authentication/billing route.

Anthropic clarification has been requested; a response is pending. Whether
subscription-profile migration is supported, especially in a future hosted
service, remains unresolved. Copying a whole directory still moves credentials,
but this plan does not declare the operation approved or prohibited. Incorporate
the response before making support claims or treating re-login as a policy
requirement. This need not block generic handoff or API-key support.

## Security and compatibility requirements

- Portable mode must not silently fall back to native execution when unavailable.
- Restrict mounts to the selected workspace and explicitly granted resources.
  Do not mount the home directory, host runtime socket, or broad credential stores.
- Establish a documented container policy for users/capabilities, resource limits,
  network access, and dependency installation. Installing tools inside the
  container must not grant control of the host.
- Keep secrets out of reusable image layers, image configuration, transfer logs,
  and ordinary bundles. Supply credentials through a deliberate runtime mechanism.
  Treat agent-created login files and transcripts as potentially sensitive; do not
  assume deleting a file later removes it from earlier image checkpoints.
- Treat images and archives as untrusted input. Authenticate transfers, validate
  manifests, extract safely, and protect local/remote artifacts with appropriate
  permissions and retention. Do not execute repository hooks during trusted import.
- Start with matching Linux image architectures. Reject unsupported combinations
  and incompatible runtime/session formats clearly before transferring ownership.
- Check provider-specific session state, extensions, MCP, browsers, and other
  integrations for portability. Explicitly block or disable unsupported features;
  do not accidentally run them on the host to make a session appear to work.
- Ordinary containers are not a complete hostile multi-tenant isolation design.
  Scope v1 to a user's trusted worker VM; public shared hosting needs separate review.

## Existing code to build on

Read the repository guidance and version-matched dependency source first.
Relevant starting points, not prescribed implementation locations:

- `src/pcode/host.py`, `remote.py`, `host_protocol.py`: detached hosts and attachment.
  The current client still reads journals/job state from local disk; forwarding a
  socket alone does not make it a remote client.
- `src/pcode/live.py`, `retries.py`, `sessions.py`: request-boundary capture,
  continuation, task-plan restoration, step persistence, and consistent session
  copying. Existing interrupted-turn recovery is not a graceful handoff protocol.
- `src/pcode/shell_tools.py`, `jobs.py`: jobs execute on the process host rather
  than through the Harness workspace backend. Run them inside the container.
- `src/pcode/worktree.py`, `tool_output_limits.py`: repository relationships and
  artifacts stored outside the session directory.
- `src/pcode/sandbox.py`, `extensions/sandbox.py`: existing native protection,
  which restricts host access but does not create a portable environment image.

## Implementation stages

Deliver and validate small vertical slices. Update this checklist as work lands.

- [ ] **Portable local execution:** project environment selection, full execution
  inside the container, retained dependencies, independent concurrent sessions,
  and explicit reset/cleanup. Preserve native-mode behavior.
- [ ] **Checkpoint and restore locally:** graceful drain, consistent image/bundle
  capture, safe import into a fresh container, and continuation mid-task. Establish
  compatibility and security checks before adding network transfer.
- [ ] **SSH worker round trip:** authenticated transfer, staged import, durable
  ownership handoff, remote detach/reattach, and transfer back without clobbering
  local changes. Prove recovery from failures before/after activation.
- [ ] **Product integration:** user-facing progress, cancellation, environment
  inspection, conflict/unsupported-feature errors, retention, and documentation.

Exact commands, configuration keys, module layout, image recipe, and wire format
are implementation choices. Avoid introducing multiple runtimes or a scheduler
unless the first working round trip requires them.

## Acceptance and verification

The main scenario: start a portable local session with several task-plan items;
install a dependency after startup; complete some work and pause partway through
another item; offload; disconnect the client; reconnect after remote progress;
bring it back and continue locally. Verify the dependency, edits, plan, history,
and referenced artifacts survive without repeating settled tool work.

Also cover:

- A new conversation reuses installed dependencies; reset returns to a known base.
- The portable launcher forwards session options without recursive container
  startup, accidental host execution, or blanket host-environment inheritance.
- API-key provisioning and, where supported, CLI-profile migration survive restart
  and return. Authentication/refresh failures block activation without losing the
  source session or silently switching authentication/billing routes.
- Concurrent environments cannot silently replace or contaminate one another.
- Native mode retains its existing behavior and Docker absence is handled clearly.
- Live jobs/workers prevent an unsafe snapshot; cancellation never implies an
  uncertain external effect is safe to retry.
- Corrupt/partial bundles, unsupported platforms, missing credentials, full disks,
  interrupted transfers, and a dropped activation acknowledgment leave recoverable
  state with at most one execution owner.
- Restart either endpoint at handoff boundaries and retry the same request without
  duplicate execution or silently losing queued input.
- Local edits during remote execution survive return; worktree pointers and
  artifact paths are valid at the destination.
- Host files and runtime sockets outside the granted policy are inaccessible;
  synthetic secret markers do not appear in images, manifests, or transfer logs.

Use deterministic tests for the lifecycle/ownership protocol and failure paths,
plus real Docker and SSH integration tests for the round trip. Follow the repo's
required terminal/socket suites when changing those areas. Record actual checks
and limitations; a mocked transport alone is not proof of portable execution.

## Reference material

These explain the underlying primitives; they do not replace pcode's handoff
protocol:

- Container filesystem commits: `https://docs.docker.com/reference/cli/docker/container/commit/`
- Image archives: `https://docs.docker.com/reference/cli/docker/image/save/`
- Image import: `https://docs.docker.com/reference/cli/docker/image/load/`
- Storage and mount behavior: `https://docs.docker.com/engine/storage/`
- Docker security: `https://docs.docker.com/engine/security/`
- Claude Code authentication: `https://code.claude.com/docs/en/authentication`
- Claude Code credential/hosted-use guidance (clarification pending): `https://code.claude.com/docs/en/legal-and-compliance`
