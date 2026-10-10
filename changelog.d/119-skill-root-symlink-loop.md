### Detect symlink-looped skill roots on Python 3.13 (#119)

`fieldkit skill install` now reports a symlink loop in a registered skill root as a validation error on Python 3.13 and later, as it already did on Python 3.11 and 3.12.
