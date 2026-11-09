# revelri — Roadmap

GitHub profile repository. Contains the profile card, hero image, and configuration for the revelri GitHub profile page.

## Current status

The October 31 card failure (undefined contribution variable) is repaired. The
generator uses public-repository commit counts and excludes unknown/private
visibility. Two offline regression tests, local SVG generation and APNG rendering
passed. Replacement assets were published manually on November 9; no CI runner was
dispatched. Scheduled-workflow execution remains separate from these local checks. Generated-card and hero commits now carry
`[skip ci]` so their pushes do not recursively start the workflow.

## Near-term

- Update badges and profile card with current projects and status
- Add a brief bio and links to active projects

## Long-term

- Automate profile card regeneration on project state changes
