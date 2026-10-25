# Authoritative evidence correction

This document supersedes the conclusions in evidence 02, 07 and 08 and the
original README, architecture, gap analysis and test plan. Historical observation
transcripts are preserved, not rerun or independently revalidated here. A quoted
`VERDICT` is harness output, not an acceptance result.

## What the existing records support

| Record | Reported observation | Defensible interpretation |
|---|---|---|
| 07 §A | Remote TCP timeout, SYN visible on destination fips0, local connection succeeds, one ip6tables ACCEPT rule | The tested remote TCP path failed. Root cause is unresolved. |
| 08 §1 | Two same-identity daemons peered with each other; zero selected log hits during short windows | A narrow two-daemon observation, not a replica-selection experiment. |
| 08 §8 | Peers present before and after graceful restart; zero selected log hits over 150 s | Restart completed in that setup. Retention of the old peer record/epoch was not verified. |
| 02 | Source inspection of master, labelled 0.6.0-dev, 244 commits past v0.5.3 | A source hypothesis, not a pinned explanation of installed 0.5.3 behaviour. |

### Firewall and TCP

An ingress tcpdump capture precedes later filtering decisions. An ACCEPT in one
ip6tables chain does not exclude other nftables base chains/hooks, priorities,
policies or return-path filtering. No complete ruleset/trace was retained here.
Do not attribute the failure to FIPS forwarding, rule out the firewall, or claim
SSH used the underlay from these observations. The attempted explanation for the
prior :8443 discrepancy is also unverified. Future diagnosis needs full rulesets,
packet traces and path-specific socket/route checks, with authorization first.

### Identity and epochs

Two nodes with the same identity peering **with each other** are not an observer
encountering two replicas. Their one session does not show a winning replica,
absence of locality, anycast viability, or an epoch-loop contradiction. Neither
anycast falsification nor confirmation follows. Zero grep hits can reflect log
levels, versions, missing preconditions or a short window, not absence of a path.

The restart script uses ordinary `kill` (SIGTERM), then waits three seconds.
Graceful shutdown can send Disconnect and remove the old peer record before the
replacement handshake. A pre-restart peer count does not establish the required
existing-record/epoch precondition at handshake time. The trace does not identify
the decision branch; the claim that the old record remained is withdrawn.

Evidence 02 names moving upstream master without an immutable commit SHA. The
source paths quoted by 08 do not establish equivalence to the installed 0.5.3
binary. Record binary digest, version, source commit and build provenance before
using source branches as explanations; none is newly verified in this correction.

### Venue and harness

Locality can be emulated on one host: give a distinct observer identity paths to
two replicas with controlled topology/netem asymmetry, then reverse the asymmetry.
That tests selection mechanics, not real geographic performance. A valid epoch
experiment also needs observer state instrumentation and verified preconditions.
The original two-daemon setup has neither. Extending its duration alone fixes
neither problem. Any new harness requires separate safety review before use.

Rootful Docker can isolate network/mount namespaces and TUN with appropriate
capabilities; host networking is not inherently required. Rootless constraints
are not evidence of Docker impossibility. MAAS is probably disproportionate for
this experiment, not fundamentally incapable. `libvirtd` inactive alone does not
prove installation or VM readiness. Fleet location and tool listings are dated
reports, not a current inventory. Stale-npub timing in 07 is a hypothesis, not a
proven smoke-harness root cause.

Both phase3 scripts are **experimental, unsafe to treat as acceptance evidence,
and not approved for rerun**. They suppress mount-isolation failures (`|| true`),
use fixed names/paths and broad `pkill -f`, and the default work path targets an
unrelated worktree. `--keep` text conflicts with unconditional namespace cleanup.
Their pass/fail text overclaims inference. This correction adds warnings only;
it does not repair, execute, or certify them. Historical claims of zero impact
remain original reports, not safety guarantees.

## Current decision

Do not deploy shared keys as a production routing mechanism on this evidence.
Keep the shared-identity question open, separately from the viable CDN design:
[unique-node architecture](ARCHITECTURE.md). A CDN does **not** require shared
IP, shared identity, multicast, or network anycast. Application selection and edge
caching/coalescing are sufficient architectural ingredients, still to implement
and validate here. Local HTTP tests are not a fleet CDN acceptance test.
