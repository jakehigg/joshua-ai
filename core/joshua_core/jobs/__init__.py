"""Jobs: work that core does on the wiki when an operator starts it.

Nothing schedules a job. An operator starts a run through
`POST /admin/jobs/{job}/run`, and `JobRunner` runs it in the background under
one lock, records it in the `job_run` table, and, when the run is not a dry
run, writes its report as a journal entry and commits the wiki. See
`docs/jobs.md`.
"""
