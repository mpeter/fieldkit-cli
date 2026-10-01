### Update urllib3 to 2.8.0 for HTTP security fixes

Installations that include optional integrations, which reach urllib3 through
`requests` or `botocore`, now lock urllib3 2.8.0. The release fixes HTTPS proxy
TLS settings that destination settings could ignore or override
(GHSA-8988-9cw3-xx77), unbounded memory use while reading chunked responses
(GHSA-vxq7-64xx-v4gw), and an infinite loop in chunked Deflate streaming
(GHSA-gh4c-6fx4-qh6g).

If you reach an integration through an HTTPS proxy that depends on custom proxy
certificates, confirm the proxy connection after upgrading: proxy TLS settings
are now applied to the proxy rather than taken from the destination.
