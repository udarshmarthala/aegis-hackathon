/**
 * Landing copy, kept as data.
 *
 * Separating it from markup means claims about the system live in one place and
 * can be checked against the code that implements them. Every guarantee below
 * corresponds to something enforced in the backend, not to an aspiration.
 */

export const PIPELINE = ['Signal', 'Cause', 'Code', 'Fix', 'Proof', 'Action'] as const;

export interface Capability {
  title: string;
  body: string;
}

export const CAPABILITIES: Capability[] = [
  {
    title: 'Investigate',
    body: 'Correlate alerts, metrics, traces, logs, topology, deployments and incident history into a hypothesis stack where every claim cites the evidence that supports it.',
  },
  {
    title: 'Localise',
    body: 'Narrow from affected service to endpoint, repository, recent commits, changed files and symbols, instead of handing an entire codebase to a model and hoping.',
  },
  {
    title: 'Reproduce',
    body: 'Run the failure and the candidate fix in a disposable container with no network, no inherited environment and no production credentials of any kind.',
  },
  {
    title: 'Verify',
    body: 'Treat a remediation as a claim that must be proven against the original incident condition, with protected metrics checked for regressions at the same time.',
  },
  {
    title: 'Act safely',
    body: 'Put deterministic policy, risk tiers, approvals, resource leases, kill switches and rollback plans between the reasoning and anything that touches production.',
  },
  {
    title: 'Learn',
    body: 'Write verified outcomes into structured incident memory so recurring patterns surface during the next investigation — and refuse to store anything unverified.',
  },
];

export interface Guarantee {
  claim: string;
  why: string;
}

export const GUARANTEES: Guarantee[] = [
  {
    claim: 'A model can never authorise its own production action.',
    why: 'An agent produces a proposal. Only the gate chain can mint the type an executor accepts, and agents have no way to construct it.',
  },
  {
    claim: 'A conclusion with no evidence is rejected, not softened.',
    why: 'Citations are re-validated against stored evidence. A diagnosis that cites an id the model invented becomes an abstention.',
  },
  {
    claim: '"We found nothing" never masquerades as "we could not look".',
    why: 'An unreachable source is recorded as an evidence gap and lowers confidence. It is rendered distinctly, end to end.',
  },
  {
    claim: 'An observability outage cannot read as a healthy service.',
    why: 'A verification claim whose metric could not be measured resolves as unavailable, and an unavailable claim can never reach a verified verdict.',
  },
  {
    claim: 'Some actions have no executable path at all.',
    why: 'Destructive operations are representable so policy can name and block them, but no executor is registered — there is nothing to call.',
  },
  {
    claim: 'An expired approval is not an approval.',
    why: 'Authorisation is re-checked immediately before the write, and an approval that lapsed while queued escalates instead of running.',
  },
  {
    claim: 'Abstention is a first-class answer.',
    why: 'Insufficient evidence is reported as insufficient evidence. The benchmark scores over-confidence and over-abstention alike.',
  },
];
