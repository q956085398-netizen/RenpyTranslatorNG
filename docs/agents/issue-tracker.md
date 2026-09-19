# Issue tracker: Local Markdown

Issues and specs for this repo live as Markdown files in `.scratch/`.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`
- The spec is `.scratch/<feature-slug>/spec.md`
- Implementation issues are one file per ticket at `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01`
- Triage state is recorded as a `Status:` line near the top of each issue file
- Comments and conversation history are appended under a `## Comments` heading

## Publishing

When a skill says "publish to the issue tracker", create the appropriate file under `.scratch/<feature-slug>/`, creating the directory when needed.

## Fetching tickets
Read the referenced Markdown file. The user will normally provide its path or issue number.

## Wayfinding operations

- Map: `.scratch/<effort>/map.md`
- Child ticket: `.scratch/<effort>/issues/NN-<slug>.md`
- Ticket type is recorded with `Type:`
- Ticket state is recorded with `Status:`
- Dependencies are recorded with `Blocked by: NN, NN`
- Claim a ticket by changing its status to `claimed`
- Resolve it by appending an `## Answer`, changing its status to `resolved`, and adding a context pointer to the map
