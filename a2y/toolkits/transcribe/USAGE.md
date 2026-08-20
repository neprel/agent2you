# Long-form transcription

This toolkit is for recordings and large audio files, not short chat voice
notes. Download the attachment from the original post into `/work`, then run:

```sh
a2y-transcribe /work/meeting.m4a --language ru --speakers 2 --output /work/meeting
```

The command is fully offline and writes `meeting.md` plus structured
`meeting.json`. Models come from the read-only host store at `/models`; this
agent's diarization tier is **{{A2Y_DIARIZATION_TIER}}**. `fallback` means
ungated MFCC clustering assigns speakers; `community-1` means the pulled
pyannote quality model assigns them. Use `host_access.gpus: all` when the host
exposes a compatible GPU; CPU is supported but a long meeting can take roughly
its own duration. Run `a2y-transcribe --check` before a costly job; it loads the
pulled models once with network disabled and prints the tier.

If the command reports `model X not in /models`, do not download anything.
Tell the operator exactly: `model X not in /models — run a2y models pull
<agent>`, substituting your own agent name.

Reply as a THREAD on the original file post. Put a concise summary and action
items inline; attach the transcript file instead of flooding the channel. Push
actions to Boards or Playbooks only when those MCPs exist and the request
authorizes it. Infer real names only from strong conversational evidence;
otherwise retain `Speaker-1`, `Speaker-2`, and so on. Speaker labels may be unreliable
for speakerphone, overlapping speech, and room recordings; say so in the result,
especially when the store tier is `fallback`.

Recording consent and legal compliance are the deploying recorder's
responsibility. Treat recordings, transcripts, model inputs and persistent
outputs as sensitive data; do not send them to cloud services unless the
operator explicitly chose that path.

Telegram Bot API downloads are limited to about 20 MB, so long recordings may
not reach the agent through Telegram even if the client accepted the upload.
Use Mattermost or an operator-provided workspace file for large meetings.
Never expand this toolkit into audio acquisition, live delivery, or automated
conference participants as a workaround.
