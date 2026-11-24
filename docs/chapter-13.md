# Chapter 13 — A Worked Orchestration

Every chapter so far has built a mechanism in isolation. We saw routing in Chapter 6, delegation in Chapter 5, memory in Chapter 7, skills in Chapter 8, MCP in Chapter 9, local-versus-cloud routing in Chapter 10, guardrails in Chapter 11, and the queue in Chapter 12. Each chapter demonstrated its mechanism with small, local examples and deferred to “a worked orchestration” for proof that the mechanisms compose into something whole.

This is that chapter. It takes one real, end-to-end system—a feature delivery factory that turns a stream of feature requests into merged code and deployed changes—and traces a single unit of work all the way through it, naming each mechanism as it appears. By the end, the nine features should resolve into one picture, with each playing a part in the same orchestration.

Seen from above, the factory is a pipeline. Each stage moves a feature a little closer to merge and deployment.

1. **Architecture** produces the feature’s blueprint, including the source list, the issue plan, and the generation standard.
2. **Drafting** turns an issue brief entry into prose.
3. **Compile-verify** confirms that the prose builds into a PDF.
4. **Code-verify** confirms that the issue’s code listings are correct.
5. **Review** judges the draft against the quality standard.
6. **Figures** produces the issue’s diagrams and images.
7. **Merge/deploy** pushes the finished feature to its distribution channels.

The stages have an order, with room for feedback. Review can send an issue back to drafting, and compile-verify loops until it passes. That flexibility gives the pipeline the shape of a graph with feedback loops.

At each stage, the control plane decides how to do the work. Each stage is a routing decision (Chapter 6). The coordinator looks at the work, classifies it, and chooses whether to act directly, delegate to a CLI, or escalate to a human. Drafting a feature issue is high-context, moderate-stakes work, so the coordinator delegates it to a capable agent with the issue’s full brief. Compile-verify is a mechanical check the coordinator can run directly. Merge/deploy is a high-stakes, irreversible action that requires human approval. Routing each stage to the right executor puts the chapter’s thesis—“orchestrate, don’t replace”—into practice.

The factory also maintains state across the whole workflow. It remembers which issues have been drafted, reviewed, and merged. It tracks outstanding figures and carries the standards that govern generation. That memory lets the pipeline run over weeks, survive restarts, and let a fresh agent pick up where the last one stopped. Durable state is what makes the factory a standing system.

Figure 13.1 puts the whole factory in one view, showing the queue, the routed stages, the verification loops, the durable state, and the two human approval gates.

## 13.3 Where Every Mechanism Appears

The worked case makes the abstract mechanisms concrete by showing each one doing a specific job in the pipeline.

- **Routing (Chapter 6)** determines who does the work at each stage. The coordinator runs mechanical checks directly, delegates authoring, and escalates review and merge to a human.
- **Delegation (Chapter 5)** is how the coordinator hands authoring and fixes to a capable agent. The agent receives an assembled brief, and the coordinator captures the outcome and writes it back to the project state.
- **Memory (Chapter 7)** carries project state across runs and agents. It records which issues are done, which decisions are locked, and which terminology to use. Each issue stays consistent with the ones before it, and a fresh agent can resume the work.
- **Skills (Chapter 8)** are repeatable procedures. The factory registers `assemble-a-brief`, `run-the-build`, and `verify-listings` as capabilities.
- **MCP (Chapter 9)** is how the merge/deploy stage reaches the distribution stores and how a status check reads the trackers. Reads can proceed freely. An irreversible write goes through a single approval point.
- **Local-versus-cloud routing (Chapter 10)** determines where inference runs at each stage. Drafting may justify a cloud model. Mechanical verification needs no model at all. The gateway meters inference against the authoring budget.
- **Guardrails (Chapter 11)** make the verification stages safe to run unattended. The code-verify sandbox contains a listing that misbehaves. The merge gate puts a human in the loop for a high-stakes action.
- **The queue (Chapter 12)** holds the per-issue tasks. It lets multiple authoring agents work on different issues in parallel without colliding, and it survives the restarts that a weeks-long feature project inevitably encounters.

For each stage, answer these four questions.

- [ ] **Routing.** Does the coordinator do the work directly, delegate it, or escalate it to a human?
- [ ] **Location.** If the stage needs inference, does it run locally or in the cloud?
- [ ] **Gate.** Does the stage need a human? If so, is that because of the stakes, the judgment required, or both?
- [ ] **Verification.** Is there a deterministic check the stage can repeat until it passes?

Then mark where memory must persist across stages, where the queue holds work, and which actions are irreversible.

A **software-release factory** has the same anatomy. The unit of work is a change, and the stages are build, test, review, stage, and deploy. In the verification loop, an agent addresses a failing build or test suite and reruns the check. Code review is the review gate. The deploy gate requires a human to authorize the irreversible push to production.

A **bug-fix factory** turns a stream of defect reports into patches. The unit of work is a bug, and the stages are reproduce, scope, patch, test, review, and merge. The verification loop relies on the test suite, which the chapter describes as a stronger oracle than prose but a weaker one than a compiler. Merging the fix is the irreversible action that requires approval.

A **data-pipeline factory** runs extraction, validation, transformation, and load. During validation, the pipeline checks the data against a schema and repeats the check after corrections. Loading the data into a production warehouse is the irreversible step that requires approval.

## 13.10 A Second Worked Case: A Software-Release Factory

The feature delivery factory is a deliberately concrete example. To see how its architecture generalizes, trace a second case in a domain familiar to most readers. A software-release factory takes a merged change all the way to a deployed service. The same architecture carries different work, with a stronger oracle and higher stakes than the feature pipeline.

The unit of work is a merged pull request. It moves through six stages.

1. **Build.** Compile and package the change.
2. **Test.** Run the test suite.
3. **Security scan.** Check dependencies and run static analysis.
4. **Stage.** Deploy to a staging environment and run smoke tests.
5. **Review.** A human approves the release.
6. **Deploy.** Roll out the release to production.

Each stage is a routing decision, just as in the feature factory. The coordinator runs the build and test suite directly. When a test fails, its output identifies what broke and gives a delegated agent a specific problem to fix. The coordinator reruns the checks after each attempt. If the test still fails when the agent reaches its attempt limit, the coordinator escalates.

The security scan is read-only. Serious findings go to a human because deciding how to handle them requires judgment. Staging applies the isolation principle from Chapter 11. The change runs in a non-production environment, where smoke tests provide another deterministic oracle.

The two human gates reflect the stakes and judgment each decision requires. Both carry higher stakes than their counterparts in the feature factory.

- **Review is the soft gate.** A human approves the release. While one release waits for review, others can proceed.
- **Production deployment is the hard gate.** The autonomy-to-stakes rule from Chapters 6 and 15 reserves this irreversible, high-stakes action for a human.

The agent does everything up to deployment. It builds, tests, scans, stages, verifies the smoke tests, and assembles the release notes. It presents a ready-to-ship release with the evidence from each check. A human takes the deploy step. This is what “production is never fully autonomous” means in practice. The entire pipeline is designed around that final decision.

The most instructive difference between the factories is the strength of their oracles. The release factory has a compiler, a test suite, a security scanner, and smoke tests. Together, these deterministic checks carry most of the quality burden. Human review can be light and focus on the judgment the tests cannot encode. Is this the right change to ship now?

In the feature factory, a successful LaTeX build establishes that the issue compiles. Judging whether the issue is good falls to the reviewer, so the review gate carries more of the quality burden.

Both factories use the same architecture. To design your own, start with the question from the previous section. What can you check deterministically? The answer determines how much work the verification loops can do and where human judgment is needed.

## §13.7 worked exercise · From a bug report to a reviewed fix

**Mike’s decisions.** For a feature that needs a blueprint, the coordinator delegates synthesis of the sources to a cloud model. Mike reviews the plan before drafting starts. Each requirement gets an ID, and the coordinator flags any ID with no corresponding issue. Mike judges whether the issue actually satisfies the requirement. A routine bug fix can go straight from its approved brief to coding and automated checks, with human approval at merge. For a large or complex change, he wants to see the proposed approach before coding. Any change to stored data or any database migration requires his approval before an agent starts coding, even when the patch is small.

**Worked example.** A report shows a 10% discount applied to $100 as $99. The issue states that the expected total is $90. The following trace applies the decisions above to that example.

| Stage | Who acts and where | What lets the work move on |
| --- | --- | --- |
| Scope | The coordinator queues the issue. For a larger pricing change, a supervised cloud agent drafts the blueprint if project privacy permits. | The coordinator checks requirement IDs against issues. Mike approves the larger plan. The routine arithmetic fix can proceed from its existing brief. |
| Reproduce | The coordinator runs a regression test locally. No model is needed. | The test expects $90 and fails on the current $99 result. That failure shows the test can detect the reported bug. |
| Patch | A supervised coding agent receives the issue and failing assertion. In this example its model runs in the cloud, if the project permits, and it edits a private working copy on the Mac. | The agent returns a diff tied to the issue. The change stays in the working copy. |
| Verify | The coordinator reruns the same test locally, plus the existing pricing tests. | The $90 case and the suite must pass. On failure, return the specific assertion to the agent. If the attempt limit is reached, send the evidence to a person. |
| Review and merge | Mike reviews the diff and test output for the exact revision proposed for the team’s integration branch. | The tests establish the arithmetic. Mike decides whether the change belongs on that branch and authorizes the merge. Production release is a separate decision if the team’s process includes it. |

**What survives a restart.** Keep the issue ID, expected result, approved scope, current revision, failing and passing test output, attempt count, and approval against the revision. The queue records whether the issue is ready for work, waiting for a fix, or waiting for review. With that record, a new worker can resume at the failed check, and the reviewer can tell which change was tested. Merge is the first shared-branch write in this example.

## Reading closeout · §§13.11–13.16

- In customer support, a policy check can catch an unauthorized promise, but judging whether a reply actually helps may require a human. Reopened tickets give the team evidence about reply quality.
- A change spanning a service and its clients needs a verification step that exercises them together. Give each worker access to the repositories its task requires.
- Compare how many items enter and leave each stage. If verified fixes accumulate at review, giving the reviewer the diff, test results, and open decision together addresses that bottleneck. Extra coding workers would only lengthen the queue.

**Next reading.** Chapter 14, *Budgets and Spend Governance*, begins with §14.1, *The Accounting Seam*, on printed page 216 (PDF page 228). Carry the same bug-fix example forward. What would the coordinator record so that the cost of planning, coding, and retries can be attributed to that one fix?
