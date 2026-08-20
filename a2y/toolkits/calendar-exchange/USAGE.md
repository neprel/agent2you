# Microsoft 365 calendar discipline

This toolkit supports Microsoft Graph for Microsoft 365. On-premises Exchange
EWS is out of scope. Invoke `login` (or run the installed server with `--login`)
and complete Microsoft's device-code flow outside the container; its cache lives
under the Hermes volume.

Read free/busy before proposing or creating an event. Always state the IANA
timezone; the container clock is UTC. Never delete an event you did not create.
For an invite: propose in chat, wait for owner approval, create the event, then
send its `.ics` through the configured email channel. Calendar-triggered
apprentice actions obey the procedure's autonomy level.
