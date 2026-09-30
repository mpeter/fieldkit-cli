# fieldkit Salesforce contributor guide

Use `SFDirectClient` for synchronous Salesforce REST access. It owns connection
lifetime, authentication headers, request retries, and response validation.
Use it as a context manager so the httpx connection closes.
The client owns lifetime and operation orchestration. Its private
`_transport.py` module owns request dispatch under the shared retry policy;
`_responses.py` owns response validation and UI API projections. Extend those
homes rather than copying helpers into consumers or adding client re-exports.
`fieldkit.sf.client.API_VERSION` is the canonical REST version for client calls
and session probes; do not maintain a separate probe version.

## Authentication and errors

Import Salesforce exceptions from `fieldkit.sf.errors`. This lightweight leaf
does not import the HTTP client or optional dependencies, so error handling need
not initialize an integration. The package and client do not re-export errors.

The supported credential is an explicitly supplied, REST-capable Salesforce
`sid` from an authorized session. `fieldkit auth sf` stores it in the active
configuration root. `fieldkit.config.get_sf_rest_base_url()` owns organization
URL selection and REST normalization: `sf_org_url` in `config.yaml` takes
precedence over `salesforce.org_url` in account configuration. Reuse that
accessor rather than duplicating precedence in consumers.
Session lifetime depends on the deployment's policy;
do not promise a fixed duration or assume a browser session grants REST access.

The client sends the credential as a Bearer authorization header. Do not log
that header, the cookie, or raw session data. fieldkit does not ship an OAuth
connected app or mint a contributor's Salesforce credentials.

`SFAuthError` inherits from `fieldkit.errors.AuthError` and reaches the top-level
CLI handler as exit 2. Preserve authentication failures even when fetching
supplemental data: an empty result must not disguise an expired session.
`SFNotFoundError` distinguishes missing records, and `SFDataAccessError`
distinguishes denied data access where a method supports it. Consult the called
method's contract before interpreting an empty collection as complete.

## Query and deployment boundaries

The client uses SOSL, direct sObject REST requests, and UI API relationship
routes. Reuse the existing methods rather than adding a second transport or
assuming a deployment permits SOQL. Keep opportunity searches scoped to
`IN NAME FIELDS`; searching all fields can match another account mentioned
inside free-text next steps.

`fetch_record()` targets opportunities; `fetch_sobject()` accepts an object
type. UI API collection results can carry completeness and issue information.
Preserve that information through consumers rather than converting an incomplete
response into a successful empty list.

The current services filter uses the custom fields `Consulting_Total_USD__c`
and `Training_Total_USD__c`. These are deployment-specific assumptions, not
standard Salesforce fields or a portable definition of services revenue.
Keep the filter in its canonical client constant; do not duplicate it in
commands. Changes require fixtures covering the supported deployment mapping,
including accounts with no matching services opportunities.

## Request policy

Reuse the client's retry and timeout behavior. Authentication failures must
escape without retrying them as transient network failures. Do not layer a
second retry loop around client calls or narrow the shared transient-status
policy to make a test pass.

New domain authentication exceptions inherit from `AuthError`; CLI adapters
must not recognize them by class-name strings or implement their own exit-code
mapping.
