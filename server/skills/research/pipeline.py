"""The deep research job: plan, search, read, extract, write, save.

Runs in the worker as a ``research`` task (started by ``start_research``):

1. **Plan**: the worker model turns the topic into 3-6 sub-questions.
2. **Search** each sub-question with the ``[search]`` provider (raw results).
3. **Read** the best pages in full (``fetch.py``); failed and paywalled pages are skipped.
4. **Extract** findings: for each page the model returns claims, each with a
   verbatim quote from that page. A finding is kept only if its quote really is
   in the fetched text, so every claim traces to a page that was read; lines
   that look like instructions aimed at an AI are never used.
5. **Write**: the model turns the findings into a summary and one section per
   sub-question; every statement must cite findings' sources, statements
   without a valid citation are dropped, and the source list is built here,
   not by the model. What couldn't be confirmed gets its own section.
6. **Save** to ``reports`` (per user) with progress along the way.

Rules it keeps:

- No claims from the model's memory: the model only sees fetched text and the
  findings drawn from it, and is told to use nothing else.
- Fetched pages are untrusted: they're wrapped in markers, the model gets no
  tools at all during research, and instruction-like text is dropped.
- Limits per depth (``[research.<depth>]``): searches, pages and a time budget.
  When one is reached the job stops gathering and writes up what it has, saying
  which limit was hit.
- If search or the worker model is unavailable, the job fails with a clear
  message rather than writing a report from memory. If no page could be read,
  it fails too.
- Cancelling (``cancel_research``) is checked between steps and between pages.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from jarvis.config import ResearchConfig, ResearchLimits
from jarvis.llm import AllModelsFailed, ChatModel
from jarvis.reading.fetch import PageFetcher, looks_like_injection
from jarvis.reading.gather import (
    UNTRUSTED_RULES,
    Gatherer,
    NothingFound,
    NothingReadable,
    SearchDown,
    Source,
    neutralize,
    page_block,
    parse_json,
    quote_on_page,
    strip_refs,
)
from jarvis.search_providers import SearchProvider
from jarvis.skills import TaskCancelled
from jarvis.store import Row, Store

MIN_QUOTE_CHARS = 15


class ResearchFailed(Exception):
    """The job can't produce an honest report. ``str(e)`` is told to the user."""


MODEL_UNAVAILABLE = (
    "The worker model is unavailable right now ({reason}), so I stopped rather than write a "
    "report without it. Try again later."
)

# Prompts

PLAN_PROMPT = """You plan web research for a busy executive. Break the topic into {n}
sub-questions that together answer it: specific, self-contained, each searchable on its own,
covering different angles (facts and figures, recent developments, players, risks or debates,
what it means). Also give a short report title (under 10 words).

Reply with JSON only: {{"title": "...", "sub_questions": ["...", "..."]}}"""

EXTRACT_PROMPT = """You extract findings from web pages for a research report. Use ONLY the
pages given; your own knowledge is not allowed, even to fill gaps.

""" + UNTRUSTED_RULES + """

Sub-questions:
{questions}

For each page, list the facts it states that help answer a sub-question. For each finding give:
- "source": the page number, e.g. 2
- "question": the sub-question number it answers
- "claim": the fact in one plain sentence, faithful to the page (keep numbers, dates, names)
- "quote": a short verbatim excerpt from that page (copied exactly, 15-300 characters) that
  supports the claim

Skip opinions dressed as fact unless attributed ("X says..."). If a page has nothing relevant,
give nothing for it. Reply with JSON only: {{"findings": [ ... ]}}"""

WRITE_PROMPT = """You write a research report for a busy executive from numbered findings.
Every finding was extracted from a web page that was read, and cites its source as [n].

Rules (strict):
- Use ONLY the findings in the user's message. Do not add facts, numbers, names or dates from
  your own knowledge, and don't speculate.
- Every statement must list the source numbers that support it, taken from the findings it
  rests on. A statement whose sources you can't name must be left out.
- Keep each finding's meaning exactly: its numbers, dates and qualifiers ("projected",
  "estimated", "expected", "according to X") stay as they are. A forecast is never a fact.
- When findings disagree, say so plainly in a statement that cites both sides.
- Say what couldn't be confirmed: sub-questions the findings don't answer, or only partly.

Reply with JSON only:
{"title": "short report title",
  "spoken_summary": "2-3 plain sentences a voice assistant can read aloud, only facts from the findings, no source numbers",
  "summary": [{"text": "key point", "sources": [1, 3]}],
  "sections": [{"question": 1, "points": [{"text": "...", "sources": [2]}]}],
  "disagreements": [{"text": "...", "sources": [2, 5]}],
  "unconfirmed": ["what is still open or unverified"]}"""

WRITE_INPUT = """Topic: {topic}

Sub-questions:
{questions}

Findings:
{findings}"""


@dataclass
class Finding:
    source: int  # index into ResearchJob.sources
    question: int  # 0-based
    claim: str
    quote: str


class ResearchJob:
    def __init__(
        self,
        *,
        store: Store,
        report: Row,
        llm: ChatModel,
        search: SearchProvider,
        fetcher: PageFetcher,
        config: ResearchConfig,
        clock: Callable[[], float] = time.monotonic,
        today: str | None = None,
    ):
        self.store = store
        self.report_id = report["id"]
        self.topic = report["topic"]
        self.depth = report["depth"]
        self.llm = llm
        self.search = search
        self.fetcher = fetcher
        self.config = config
        self.limits: ResearchLimits = config.for_depth(self.depth)
        self.clock = clock
        self.today = today or datetime.now().strftime("%d %B %Y")
        self.title = ""
        self.questions: list[str] = []
        self.findings: list[Finding] = []
        self.model_calls = 0
        self.g = self._gatherer()

    def _gatherer(self) -> Gatherer:
        return Gatherer(
            search=self.search,
            fetcher=self.fetcher,
            max_searches=self.limits.max_searches,
            max_pages=self.limits.max_pages,
            time_budget_secs=self.limits.time_budget_secs,
            results_per_search=self.config.results_per_search,
            pages_per_query=self.config.pages_per_question,
            unit="sub-question",
            check=self._check_cancelled,
            progress=self._progress,
            clock=self.clock,
        )

    @property
    def sources(self) -> list[Source]:
        return self.g.sources

    # The job

    async def run(self) -> str:
        """Run the job and save the report. Returns the spoken summary.

        Raises TaskCancelled if the user cancelled it, ResearchFailed (after
        saving the failure on the report) if no honest report can be written.
        """
        started = await self.store.update_report(
            self.report_id,
            {"status": "running", "started_at": _now(), "progress": 2, "progress_note": "Planning"},
            only_if_status=("queued",),
        )
        if started is None:
            raise TaskCancelled()
        self.g = self._gatherer()  # the time budget starts now
        try:
            await self._plan()
            await self._gather()
            await self._extract()
            content, body, spoken = await self._write()
        except (TaskCancelled, ResearchFailed):
            raise
        except AllModelsFailed as e:
            raise await self._fail(MODEL_UNAVAILABLE.format(reason=_short(e))) from e
        except Exception as e:
            logger.exception(f"Research {self.report_id} crashed")
            raise await self._fail(f"The research job hit an unexpected error: {_short(e)}") from e

        saved = await self.store.update_report(
            self.report_id,
            {
                "status": "done",
                "progress": 100,
                "progress_note": "Done",
                "title": content["title"],
                "spoken_summary": spoken,
                "content": content,
                "body_text": body,
                "stats": self._stats(),
                "finished_at": _now(),
            },
            only_if_status=("running",),
        )
        if saved is None:
            raise TaskCancelled()
        logger.info(f"Research {self.report_id} done: {self._stats()}")
        return spoken

    # 1. Plan

    async def _plan(self) -> None:
        n = self.limits.sub_questions
        try:
            data = parse_json(await self._ask(PLAN_PROMPT.format(n=n), f"Topic: {self.topic}"))
            questions = [str(q).strip() for q in data.get("sub_questions") or [] if str(q).strip()]
            self.title = str(data.get("title") or "").strip()[:120]
        except ValueError:
            questions = []
        if len(questions) < 3:
            logger.warning(f"Research {self.report_id}: plan unusable; using generic sub-questions")
            questions = _fallback_questions(self.topic)
        self.questions = questions[: max(3, min(6, n))]
        self.title = self.title or self.topic[:120]
        await self._progress(10, f"Planned {len(self.questions)} sub-questions")

    # 2 and 3. Search and read (jarvis.reading.gather)

    async def _gather(self) -> None:
        try:
            await self.g.search(self.questions)
        except SearchDown as e:
            raise await self._fail(f"Web search is unavailable, so I couldn't research this: {e}") from e
        except NothingFound as e:
            raise await self._fail(
                "The web search found nothing for this topic, so there's no report to write."
            ) from e
        try:
            await self.g.read()
        except NothingReadable as e:
            raise await self._fail(
                f"I found {e.found} sources but couldn't read any of the {e.tried} I tried (they "
                "failed to load or are paywalled), so there's no report."
            ) from e

    # 4. Extract

    async def _extract(self) -> None:
        read = [i for i, s in enumerate(self.sources) if s.read]
        batches: list[list[int]] = [[]]
        size = 0
        for i in read:
            length = len(self.sources[i].page.text)  # type: ignore[union-attr]
            if batches[-1] and size + length > self.config.extract_batch_chars:
                batches.append([])
                size = 0
            batches[-1].append(i)
            size += length
        questions = _numbered(self.questions)
        for bi, batch in enumerate(batches):
            await self._check_cancelled()
            # The first batch always runs, so pages already read aren't wasted when
            # time ran out while reading; later batches respect the budget.
            if bi > 0 and self.g.out_of_time():
                unread = sum(len(b) for b in batches[bi:])
                self.g.hit_limit(f"time budget reached while extracting ({unread} page(s) not analysed)")
                break
            pages = "\n\n".join(page_block(n, self.sources[i]) for n, i in enumerate(batch, 1))
            reply = await self._ask(EXTRACT_PROMPT.format(questions=questions), pages)
            try:
                raw = parse_json(reply).get("findings") or []
            except ValueError:
                logger.warning(f"Research {self.report_id}: unreadable findings from batch {bi + 1}")
                raw = []
            for item in raw if isinstance(raw, list) else []:
                finding = self._validated(item, batch)
                if finding is not None:
                    self.findings.append(finding)
            await self._progress(
                65 + int(20 * (bi + 1) / len(batches)),
                f"Found {len(self.findings)} findings so far",
            )

    def _validated(self, item: Any, batch: list[int]) -> Finding | None:
        """A finding the page really supports, or None."""
        if not isinstance(item, dict):
            return None
        try:
            n, q = int(item.get("source")), int(item.get("question"))
        except (TypeError, ValueError):
            return None
        claim, quote = str(item.get("claim") or "").strip(), str(item.get("quote") or "").strip()
        if not (1 <= n <= len(batch) and 1 <= q <= len(self.questions)) or not claim:
            return None
        source = self.sources[batch[n - 1]]
        if len(quote) < MIN_QUOTE_CHARS or not quote_on_page(source.page.text, quote):  # type: ignore[union-attr]
            return None  # not actually on the page: maybe from the model's memory
        if looks_like_injection(quote) or looks_like_injection(claim):
            return None
        return Finding(source=batch[n - 1], question=q - 1, claim=claim[:500], quote=quote[:300])

    # 5. Write

    async def _write(self) -> tuple[dict, str, str]:
        await self._check_cancelled()
        await self._progress(88, "Writing the report")
        if not self.findings:
            return self._empty_report()

        cited = sorted({f.source for f in self.findings})
        number = {src: k for k, src in enumerate(cited, 1)}  # source index -> [n] for the writer
        lines = []
        for qi in range(len(self.questions)):
            for f in (f for f in self.findings if f.question == qi):
                src = self.sources[f.source]
                dated = f", published {src.published}" if src.published else ""
                lines.append(
                    f"- Q{qi + 1} [{number[f.source]}] {f.claim} "
                    f"(\"{neutralize(f.quote)}\" - {neutralize(src.title)}{dated})"
                )
        material = WRITE_INPUT.format(
            topic=self.topic, questions=_numbered(self.questions), findings="\n".join(lines)
        )
        try:
            data = parse_json(await self._ask(WRITE_PROMPT, material))
        except ValueError:
            logger.warning(f"Research {self.report_id}: report JSON unusable; listing findings")
            data = {}
        return self._assemble(data, number)

    def _assemble(self, data: dict, number: dict[int, int]) -> tuple[dict, str, str]:
        valid = set(number.values())
        by_number = {n: src for src, n in number.items()}

        def points(items: Any) -> list[dict]:
            out = []
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or "").strip()
                refs = []
                for r in item.get("sources") or []:
                    try:
                        if int(r) in valid and int(r) not in refs:
                            refs.append(int(r))
                    except (TypeError, ValueError):
                        continue
                if text and refs and not looks_like_injection(text):
                    out.append({"text": strip_refs(text), "sources": refs})
            return out

        summary = points(data.get("summary"))
        sections = []
        for qi, question in enumerate(self.questions):
            section_points: list[dict] = []
            for sec in data.get("sections") or []:
                try:
                    if isinstance(sec, dict) and int(sec.get("question")) == qi + 1:
                        section_points += points(sec.get("points"))
                except (TypeError, ValueError):
                    continue
            if not section_points:
                # The writer gave nothing usable for a sub-question that has findings:
                # list the (quote-checked) findings themselves rather than drop them.
                section_points = [
                    {"text": f.claim, "sources": [number[f.source]]}
                    for f in self.findings if f.question == qi
                ]
            sections.append({"question": question, "points": section_points})
        if not summary:
            summary = [p for s in sections for p in s["points"][:1]][:4]
        disagreements = points(data.get("disagreements"))

        unconfirmed = [str(u).strip() for u in data.get("unconfirmed") or [] if str(u).strip()]
        unconfirmed = [u for u in unconfirmed if not looks_like_injection(u)][:8]
        for sec in sections:
            if not sec["points"]:
                unconfirmed.append(f"No source I read answered: {sec['question']}")
        unconfirmed += self._gap_notes()

        # Renumber sources in order of first citation, and build the list here.
        order: list[int] = []
        for p in [*summary, *(p for s in sections for p in s["points"]), *disagreements]:
            for r in p["sources"]:
                if r not in order:
                    order.append(r)
        renumber = {old: new for new, old in enumerate(order, 1)}
        for p in [*summary, *(p for s in sections for p in s["points"]), *disagreements]:
            p["sources"] = [renumber[r] for r in p["sources"]]
        sources = [self._source_view(self.sources[by_number[old]], new) for old, new in renumber.items()]
        cited_ids = {by_number[old] for old in order}
        also_read = [self._source_view(s) for i, s in enumerate(self.sources)
                     if s.read and i not in cited_ids]

        title = str(data.get("title") or "").strip()[:120] or self.title
        spoken = " ".join(str(data.get("spoken_summary") or "").split())
        if not spoken or looks_like_injection(spoken):
            spoken = " ".join(p["text"] for p in summary[:2]) or f"The report on {self.topic} is ready."
        content = {
            "title": title,
            "topic": self.topic,
            "depth": self.depth,
            "date": self.today,
            "summary": summary,
            "sections": sections,
            "disagreements": disagreements,
            "unconfirmed": unconfirmed,
            "sources": sources,
            "also_read": also_read,
            "unreadable": self._unreadable(),
            "limits_hit": list(self.g.limits_hit),
        }
        return content, render_text(content), spoken

    def _empty_report(self) -> tuple[dict, str, str]:
        read = sum(1 for s in self.sources if s.read)
        content = {
            "title": self.title,
            "topic": self.topic,
            "depth": self.depth,
            "date": self.today,
            "summary": [],
            "sections": [{"question": q, "points": []} for q in self.questions],
            "disagreements": [],
            "unconfirmed": [f"No source I read answered: {q}" for q in self.questions]
            + self._gap_notes(),
            "sources": [],
            "also_read": [self._source_view(s) for s in self.sources if s.read],
            "unreadable": self._unreadable(),
            "limits_hit": list(self.g.limits_hit),
        }
        spoken = (
            f"I read {read} pages about {self.topic} but none of them clearly answered the "
            "questions, so I couldn't confirm anything. The pages I read are listed in the report."
        )
        return content, render_text(content), spoken

    def _gap_notes(self) -> list[str]:
        notes = [f"Stopped early: {hit}." for hit in self.g.limits_hit]
        failed = [s for s in self.sources if s.failure]
        if failed:
            notes.append(f"{len(failed)} source(s) couldn't be read (failed to load or paywalled).")
        return notes + list(dict.fromkeys(self.g.notes))

    def _source_view(self, s: Source, n: int | None = None) -> dict:
        view: dict[str, Any] = {
            "url": s.page.final_url if s.page else s.url, "title": s.title, "published": s.published
        }
        if n is not None:
            view["n"] = n
        return view

    def _unreadable(self) -> list[dict]:
        return self.g.unreadable()

    # Plumbing

    async def _ask(self, system: str, user: str) -> str:
        """One model call, with no tools: research never gives the model a way to act."""
        self.model_calls += 1
        reply = await self.llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}], tools=None
        )
        return reply.text

    async def _progress(self, percent: int, note: str) -> None:
        updated = await self.store.update_report(
            self.report_id,
            {"progress": max(0, min(99, percent)), "progress_note": note},
            only_if_status=("running",),
        )
        if updated is None:
            raise TaskCancelled()

    async def _check_cancelled(self) -> None:
        report = await self.store.get_report(self.report_id)
        if report is None or report["status"] != "running":
            raise TaskCancelled()

    def _stats(self) -> dict:
        return {**self.g.stats(), "findings": len(self.findings), "model_calls": self.model_calls}

    async def _fail(self, message: str) -> ResearchFailed:
        """Save the failure on the report; returns the exception to raise."""
        await self.store.update_report(
            self.report_id,
            {"status": "failed", "error": message, "progress_note": "Failed",
             "stats": self._stats(), "finished_at": _now(),
             "content": {"unreadable": self._unreadable(), "questions": self.questions}},
            only_if_status=("running", "queued"),
        )
        return ResearchFailed(message)


# Rendering


def render_text(content: dict) -> str:
    """The report as plain text (for email and follow-up questions)."""

    def cite(p: dict) -> str:
        return p["text"] + " " + "".join(f"[{n}]" for n in p["sources"])

    out = [content["title"], f"Researched on {content['date']} ({content['depth']} depth)", ""]
    out.append("SUMMARY")
    out += [f"- {cite(p)}" for p in content["summary"]] or ["- Nothing could be confirmed."]
    for i, sec in enumerate(content["sections"], 1):
        out += ["", f"{i}. {sec['question']}"]
        out += [f"- {cite(p)}" for p in sec["points"]] or ["- Not answered by the sources read."]
    if content["disagreements"]:
        out += ["", "WHERE SOURCES DISAGREE"] + [f"- {cite(p)}" for p in content["disagreements"]]
    out += ["", "WHAT I COULDN'T CONFIRM"]
    out += [f"- {u}" for u in content["unconfirmed"]] or ["- Nothing outstanding."]
    out += ["", "SOURCES"]
    for s in content["sources"]:
        dated = f" (published {s['published']})" if s.get("published") else ""
        out.append(f"[{s['n']}] {s['title']}{dated}. {s['url']}")
    if not content["sources"]:
        out.append("- None cited.")
    return "\n".join(out)


# Helpers


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _short(e: Exception) -> str:
    return str(e)[:200] or type(e).__name__


def _numbered(items: list[str]) -> str:
    return "\n".join(f"{i}. {q}" for i, q in enumerate(items, 1))


def _fallback_questions(topic: str) -> list[str]:
    return [
        f"What is the current state of {topic}?",
        f"What are the most recent developments in {topic}?",
        f"What are the main risks, debates or open questions about {topic}?",
    ]
