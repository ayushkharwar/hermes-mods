# README decisions

- Open with a one-line purpose so readers can place the repository immediately.
- Put actual renderer output and one measured verifier result first so readers see evidence before installation.
- Use the golden session fixture with optional rows hidden so both progress samples stay compact and reproducible.
- Label fixture data and the cold keepwarm card so sample values cannot pass for live measurements.
- Add anchor navigation and a three-mod table so readers can choose a path quickly.
- Keep installation in one shell block and identify Hermes prompt commands so readers know where to paste each step.
- Show installer destinations and its enable-failure caveat so a final Done message does not imply every plugin enabled.
- Give each mod its output, commands, and limits so readers can use it without reading implementation code.
- Use tables for row meanings, explorer keys, and fallback behavior so comparisons stay easy to scan.
- Draw the hook, snapshot, widget, and replay paths so contributors can see the data contracts.
- Report the measured suite count from a normal run, 16 of 16 passing.
  The review sandbox could not write Hermes state or open localhost, so its own 12/16 is
  an artefact of that sandbox, not the repository.
- Explain active-home writes and localhost test servers because the existing root README's temp-only claim does not match the tests.
- Link only existing repository paths because filetree-rail and keepwarm have no plugin README in this checkout.
- Leave the root README and concurrent plugin edits untouched so this remains a reviewable documentation proposal.
- Use plain Markdown, short lines, and whitespace so GitHub renders the page without HTML layout tricks.

First ten seconds: the reader should feel oriented, able to inspect the evidence, and clear about what to install.
