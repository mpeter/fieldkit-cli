### Fix forecast fallback for blank consulting ACV (#74)

`fieldkit pursuit forecast` now treats blank consulting ACV as missing, so a populated net ACV is included in forecast totals. Explicit zero amounts and standard versus fixed-price component ordering remain unchanged. Malformed amounts now use the pursuit model's validation diagnostics instead of silently becoming zero.
