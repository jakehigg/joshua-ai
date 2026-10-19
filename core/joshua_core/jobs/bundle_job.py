"""The `bundle` job: build the memory bundle of one topic and report it.

It makes no model call and writes no page. Its report lists the paths in the
bundle, its size, its aliases, and whether it was cut, so an operator can see
what a later job would read about a topic. Arguments: `{"topic": "<path>"}`,
the topic page relative to the wiki root.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from joshua_shared import layout
from joshua_shared.config import JoshuaConfig

from joshua_core.jobs.bundle import BundleStore, build_bundle
from joshua_core.jobs.runner import Clock, Job, JobArgsError, RunReport
from joshua_core.jobs.topics import is_topic_page, load_topic


class BundleJob(Job):
    name = "bundle"

    def __init__(self, *, store: BundleStore, settings: JoshuaConfig, data_dir: Path | str):
        self._store = store
        self._settings = settings
        self._data_dir = Path(data_dir)

    @property
    def _wiki(self) -> Path:
        return layout.wiki_root(self._data_dir)

    def check_args(self, args: dict[str, Any]) -> dict[str, Any]:
        topic = args.get("topic")
        if not isinstance(topic, str) or not topic.strip():
            raise JobArgsError("topic is required: a wiki page path such as garden/roses.md")
        unknown = sorted(set(args) - {"topic"})
        if unknown:
            raise JobArgsError(f"unknown argument: {', '.join(unknown)}")
        topic = topic.strip()
        if not is_topic_page(topic, self._wiki, self._settings.jobs.topics.exclude):
            raise JobArgsError(f"not a topic page: {topic}")
        return {"topic": topic}

    async def run_once(self, *, args: dict[str, Any], dry_run: bool, clock: Clock) -> RunReport:
        started = clock()
        topic = load_topic(args["topic"], self._wiki)
        bundle = await build_bundle(
            topic, wiki_root=self._wiki, repo=self._store, settings=self._settings, now=started
        )
        cut = " It was cut at jobs.bundle.max_chars." if bundle.truncated else ""
        summary = (
            f"The bundle of {topic.path} holds {len(bundle.paths)} pages and "
            f"{bundle.chars} characters.{cut}"
        )
        aliases = ", ".join(bundle.aliases) if bundle.aliases else "None."
        return RunReport(
            job=self.name,
            started_at=started,
            dry_run=dry_run,
            args=args,
            summary=summary,
            sections=[
                ("Pages", "\n".join(f"- `{path}`" for path in bundle.paths)),
                ("Aliases", aliases),
            ],
            data={
                "topic": topic.path,
                "title": topic.title,
                "section": topic.section,
                "paths": bundle.paths,
                "chars": bundle.chars,
                "truncated": bundle.truncated,
                "aliases": bundle.aliases,
            },
        )
