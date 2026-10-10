### Validate `pursuit rename` names before writing (#105)

`pursuit rename --to` now requires a pursuit slug, the form `pursuit create` produces, and `--account` and `--from` can no longer contain a path separator. A value such as `../moved`, `a/b`, an empty string, or `New Deal` exits 3 without moving the file or rewriting watcher state, and the message suggests the slug form. Previously the value was used verbatim, so it could move a pursuit outside the account's `pursuits/` directory or write `pursuits/.md`.
