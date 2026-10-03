### Bind documentation rehearsals to the full-enforcement gate

The one-time `Cutover verification` workflow ran only on the repository's root
push, so no later commit could produce rehearsal evidence. It is removed.
Manual documentation rehearsals now cite a successful scheduled or dispatched
`Full enforcement` run of the exact public commit on `main`, and their contract
classification is renamed from `exact_release_cutover_proof` to
`public_rehearsal_proof` (verification ID `manual.public-rehearsal`).
