"""Google Workspace over the pipeline: Chat, Gmail, and Calendar.

A finished run can be posted to a Google Chat space, emailed with the board
file attached, and -- when the adversarial review found blockers -- turned
into a design-review Calendar event with a Meet link. Structured like
``service/`` and the CLI: stdlib only, no Google client libraries, and no
engine logic of its own. Every network call goes through an injectable
transport so the tests run offline against recorded requests.

Entry point: ``python -m googleapps`` (subcommands ``auth``, ``check``,
``run``).
"""
