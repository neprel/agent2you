# Google Calendar discipline

Enable the Google Calendar API, create a Desktop OAuth client, add the owner as
a consent-screen test user, and copy its JSON to
`volumes/agent-<name>/hermes/google-calendar-oauth.json`. Invoke the MCP's
`manage-accounts` authentication tool and complete the browser flow outside the
container. Test-mode refresh tokens may expire after seven days; doctor reports
credential expiry when the token JSON exposes it.

Read free/busy before proposing or creating an event. Always state the IANA
timezone; the container clock is UTC. Never delete an event you did not create.
For an invite: propose in chat, wait for owner approval, create the event, then
send its `.ics` through the configured email channel. Calendar-triggered
apprentice actions obey the procedure's autonomy level.
