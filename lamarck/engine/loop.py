"""One evolution run: rounds of evaluate -> propose -> screen -> evaluate -> select.

    round t
      1. F_t    <- Evaluate(H_t, evolve)                      cached by harness tree hash
      2. b_t    <- annealed edit budget; prune set from D_t
      3. C_t    <- k proposals (each drafted in its own git worktree / branch),
                   predictions SEALED to disk before any evaluation
      4. screen <- deterministic critic, then one LLM screen call (selector, reads SIM)
      5. F_t^C  <- Evaluate(c, evolve) for screened candidates, in parallel sandboxes
      6. admissible set by RRSI rules (noise floor, cost rule, guards); the LLM selector
         picks one of them or none (reads SIM)
      7. accept: fast-forward `evolve`; SIM <- U(SIM, e_t) with measured fixed/missed;
         D_t <- all candidate records; state saved (the run is resumable at any point)
      8. optional: held-out score of the retained harness (never shown to the search)

A crashed run resumes at the first unfinished round. Sealed proposals, screen
and selection decisions and evaluations are reused, so resuming spends no
extra model calls on work already done, and the SIM update is idempotent.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config import RunConfig
from ..domains.base import Domain
from ..gateway.client import GatewayClient, GatewayError
from ..sim import prompts as sim_prompts
from ..sim.memory import SIM, Attempt, files_field, truncate
from ..types import EvalResult, read_json, write_json
from . import critic, proposer, regularizers, selector
from .evaluator import BudgetStop, Evaluator
from .gitops import EVOLVE, HarnessRepo
from .history import CandidateRecord, RunHistory


@dataclass
class SearchLLM:
    """The harness optimizer's model (proposer, selector) behind the gateway."""

    client: GatewayClient
    model: str
    temperature: float = 0.8
    max_tokens: int = 8192
    extra: dict[str, Any] = field(default_factory=dict)

    def ask(
        self, role: str, messages: list[dict[str, Any]], seed: int, temperature: float | None = None
    ) -> str:
        try:
            res = self.client.chat(
                role,
                self.model,
                messages,
                temperature=self.temperature if temperature is None else temperature,
                max_tokens=self.max_tokens,
                seed=seed,
                **self.extra,
            )
        except GatewayError as exc:
            if exc.budget_exceeded:
                raise BudgetStop(str(exc)) from exc
            raise
        return res.content


@dataclass
class Candidate:
    proposal: proposer.Proposal
    branch: str
    commit: str
    tree: str
    diff: str
    files: list[str]
    gate: str = ""
    ev: EvalResult | None = None


class EvolutionRun:
    def __init__(
        self, cfg: RunConfig, domain: Domain, evaluator: Evaluator, search: SearchLLM, run_dir: Path
    ) -> None:
        self.cfg = cfg
        self.domain = domain
        self.evaluator = evaluator
        self.search = search
        self.dir = run_dir
        self.repo = HarnessRepo(run_dir / "repo")
        self.history = RunHistory(run_dir / "history.jsonl")
        self.sim_path = run_dir / "sim.json"
        self.state_path = run_dir / "state.json"
        self._log_lock = threading.Lock()
        self._git_lock = threading.Lock()  # git worktree/commit operations are not concurrency-safe

    # ------------------------------------------------------------- creation
    @classmethod
    def create(
        cls,
        cfg: RunConfig,
        domain: Domain,
        evaluator: Evaluator,
        search: SearchLLM,
        run_dir: Path,
        *,
        source: EvolutionRun | None = None,
        source_ref: str = EVOLVE,
    ) -> EvolutionRun:
        """New run from the domain's seed harness (source / Cold Start) or continued from a
        source run's final harness (Harness Transfer / Problem-Only / Full SIM / Retrieved)."""
        if (run_dir / "state.json").exists():
            return cls(RunConfig.load(run_dir / "config.json"), domain, evaluator, search, run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        cfg.save(run_dir / "config.json")
        if source is None:
            HarnessRepo.create(run_dir / "repo", domain.seed_harness)
            sim = SIM()
        else:
            HarnessRepo.clone_from(source.repo, run_dir / "repo", source_ref)
            src_sim = SIM.load(source.sim_path)
            carried = src_sim.carried(arm=source.cfg.run_id, domain=source.cfg.provenance, into=cfg.run_id)
            if cfg.memory == "problems":
                sim = carried.problems_only()
            elif cfg.memory in ("full", "retrieved"):
                sim = carried
            else:
                # no memory reaches the agents; the run still records its own attempts for analysis
                sim = SIM(carried_from=None)
        sim.save(run_dir / "sim.json")
        run = cls(cfg, domain, evaluator, search, run_dir)
        write_json(
            run.state_path, {"next_round": 0, "best_score": None, "delta": cfg.delta, "status": "created"}
        )
        return run

    # ---------------------------------------------------------------- state
    def state(self) -> dict[str, Any]:
        st: dict[str, Any] = read_json(self.state_path)
        return st

    def save_state(self, **kw: Any) -> None:
        st = self.state()
        st.update(kw)
        st["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        write_json(self.state_path, st)

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} [{self.cfg.run_id}] {msg}"
        with self._log_lock:
            with open(self.dir / "events.log", "a") as f:
                f.write(line + "\n")
            print(line, flush=True)

    def sim(self) -> SIM:
        return SIM.load(self.sim_path)

    # ----------------------------------------------------------- evaluation
    def harness_dir(self, ref: str) -> tuple[Path, str]:
        with self._git_lock:
            tree = self.repo.tree(ref)
            d = self.dir / "harness_cache" / tree
            if not d.exists():
                self.repo.export(ref, d)
        return d, tree

    def evaluate(self, ref: str, split: str) -> EvalResult:
        d, tree = self.harness_dir(ref)
        return self.evaluator.evaluate(d, tree, split, self.cfg.policy_model, self.cfg.rollout)

    def delta(self) -> float:
        st = self.state()
        if st.get("delta") is not None:
            return float(st["delta"])
        if self.cfg.delta is not None:
            return float(self.cfg.delta)
        cal = self.calibrate()
        return float(cal["delta"])

    def calibrate(self, repeats: int = 3) -> dict[str, Any]:
        """delta from `repeats` evaluations of the run's starting harness (different seeds)."""
        path = self.dir / "calibration.json"
        if path.exists():
            cal: dict[str, Any] = read_json(path)
        else:
            d, tree = self.harness_dir(EVOLVE)
            evs = [
                self.evaluator.evaluate(d, tree, self.cfg.evolve_split, self.cfg.policy_model, rollout=r)
                for r in range(repeats)
            ]
            cal = regularizers.calibrate_delta(evs, z=self.cfg.delta_z)
            cal["harness_tree"] = tree
            write_json(path, cal)
        self.save_state(delta=cal["delta"])
        self.log(f"noise band delta = {cal['delta']:.4f} from scores {cal['scores']}")
        return cal

    # ----------------------------------------------------------------- main
    def run(self) -> dict[str, Any]:
        try:
            if self.cfg.use_noise_floor or self.cfg.use_cost_rule:
                self.delta()
            self.heldout_point(-1)
            while self.state()["next_round"] < self.cfg.rounds:
                t = int(self.state()["next_round"])
                self.round(t)
                self.save_state(next_round=t + 1, status="running")
                self.heldout_point(t)
            summary = self.summary()
            self.save_state(status="done")
            return summary
        except BudgetStop as exc:
            self.log(f"BUDGET STOP: {exc}")
            self.save_state(status="budget_stop", stop_reason=str(exc))
            raise

    def heldout_point(self, after_round: int) -> None:
        """Held-out score of the retained harness. Written to heldout.jsonl, which no prompt reads."""
        is_end = after_round == self.cfg.rounds - 1
        if not (self.cfg.heldout_curve or after_round == -1 or is_end):
            return
        path = self.dir / "heldout.jsonl"
        done = (
            {json.loads(x)["after_round"] for x in path.read_text().splitlines()} if path.exists() else set()
        )
        if after_round in done:
            return
        ev = self.evaluate(EVOLVE, self.cfg.heldout_split)
        rec = {
            "after_round": after_round,
            "tree": ev.harness_tree,
            "commit": self.repo.rev(EVOLVE),
            "score": ev.score,
            "cost": ev.cost,
            "n": len(ev.outcomes),
        }
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
        self.log(
            f"held-out after round {after_round}: {ev.score * 100:.2f} ({len(ev.passed)}/{len(ev.outcomes)})"
        )

    # ------------------------------------------------------------- one round
    def memory_block(self, failing_text: str) -> str:
        mode = self.cfg.memory
        if mode == "none":
            return ""
        sim = self.sim()
        if mode == "retrieved":
            from ..sim.retrieval import retrieve

            sim = retrieve(sim, failing_text, self.cfg.retrieval_top_k, self.cfg.retrieval_token_budget)
        return sim_prompts.render(sim, mode)

    def round(self, t: int) -> None:
        cfg = self.cfg
        rdir = self.dir / "rounds" / f"r{t}"
        rdir.mkdir(parents=True, exist_ok=True)
        inc_commit = self.repo.rev(EVOLVE)
        inc = self.evaluate(EVOLVE, cfg.evolve_split)
        st = self.state()
        best = max(float(st["best_score"]) if st.get("best_score") is not None else 0.0, inc.score)
        delta = self.delta() if (cfg.use_noise_floor or cfg.use_cost_rule) else 0.0
        budget = (
            regularizers.edit_budget(t, cfg.rounds, cfg.b_min, cfg.b_max) if cfg.use_edit_budget else None
        )
        tasks = self.domain.tasks(cfg.evolve_split)
        feedback = proposer.render_feedback(self.domain, tasks, inc, cfg.max_trace_chars)
        memory_block = self.memory_block(feedback)
        prune = regularizers.prune_set(self.history.measured(), t, cfg.n_prune) if cfg.use_prune_set else []
        self.log(
            f"round {t}: incumbent {inc.harness_tree} evolve={inc.score * 100:.1f} b_t={budget} best={best * 100:.1f}"
        )

        # 3. proposals (parallel), each sealed before evaluation
        files = self.repo.read_files(EVOLVE)
        failing = set(inc.failed)

        def make(j: int) -> Candidate | None:
            return self._propose(
                t, j, rdir, inc_commit, files, feedback, memory_block, prune, budget, failing
            )

        with ThreadPoolExecutor(max_workers=cfg.k) as pool:
            cands = [c for c in pool.map(make, range(cfg.k)) if c is not None]

        # 4. screen: deterministic critic, then the LLM screen
        for c in cands:
            if cfg.use_critic and not c.gate:
                reasons = critic.screen(
                    c.diff, c.proposal.edits, c.proposal.n_edits, budget, self.domain.critic_patterns()
                )
                if reasons:
                    c.gate = "critic: " + "; ".join(reasons)[:300]
        live = [c for c in cands if not c.gate]
        if cfg.use_llm_screen and live:
            verdicts = self._llm_screen(t, rdir, live, memory_block)
            for c in live:
                v = verdicts.get(c.proposal.sample)
                if v is not None and not v.accept:
                    c.gate = "screen: " + v.reason[:300]
        live = [c for c in cands if not c.gate]

        # 5. evaluate screened candidates in parallel sandboxes
        def run_eval(c: Candidate) -> None:
            c.ev = self.evaluate(c.commit, cfg.evolve_split)

        with ThreadPoolExecutor(max_workers=max(1, cfg.eval_parallel)) as pool:
            list(pool.map(run_eval, live))

        # 6. admissibility (RRSI rules), then the LLM selector
        accepted_files = self.history.accepted_files()
        decisions: dict[str, regularizers.Decision] = {}
        for c in cands:
            guards = self.domain.guards(inc, c.ev) if c.ev is not None else []
            novelty = len(set(c.files) - accepted_files)
            decisions[c.proposal.sample] = regularizers.judge(
                c.proposal.sample, c.ev, inc, best, delta, cfg, guards, novelty, gate_failure=c.gate
            )
        admissible = [c for c in cands if decisions[c.proposal.sample].admissible]
        chosen, why = (
            self._select(t, rdir, inc, admissible, memory_block)
            if admissible
            else (None, "nothing admissible")
        )
        self.log(
            f"round {t}: {len(cands)} proposed, {len(live)} evaluated, {len(admissible)} admissible -> "
            f"{chosen.proposal.sample if chosen else 'keep incumbent'} ({why[:120]})"
        )

        # 7. records, SIM update, fast-forward
        recs = []
        for c in cands:
            d = decisions[c.proposal.sample]
            fixed = sorted(set(c.ev.passed) - set(inc.passed)) if c.ev else []
            broken = sorted(set(inc.passed) - set(c.ev.passed)) if c.ev else []
            recs.append(
                CandidateRecord(
                    round=t,
                    sample=c.proposal.sample,
                    branch=c.branch,
                    commit=c.commit,
                    tree=c.tree,
                    cite=c.proposal.cite,
                    root_cause=c.proposal.root_cause,
                    what=c.proposal.what,
                    files=c.files,
                    n_edits=c.proposal.n_edits,
                    predicted=c.proposal.predicted,
                    gate=c.gate,
                    score=d.score,
                    cost=d.cost,
                    d_score=d.d_score,
                    d_cost=d.d_cost,
                    admissible=d.admissible,
                    decision=(why if chosen is c else d.reason),
                    selected=chosen is c,
                    fixed=fixed,
                    broken=broken,
                )
            )
        self.history.write_round(t, recs)
        if chosen is not None and chosen.ev is not None:
            self._record_sim(t, chosen, chosen.ev)
            self.repo.fast_forward(chosen.commit)
            best = max(best, chosen.ev.score)
        self.save_state(best_score=best)
        write_json(rdir / "done.json", {"chosen": chosen.proposal.sample if chosen else None, "reason": why})

    # --------------------------------------------------------------- pieces
    def _propose(
        self,
        t: int,
        j: int,
        rdir: Path,
        inc_commit: str,
        files: dict[str, str],
        feedback: str,
        memory_block: str,
        prune: list[str],
        budget: int | None,
        failing: set[str],
    ) -> Candidate | None:
        sample = f"s{j + 1}"
        mpath = rdir / sample / "manifest.json"
        branch = f"cand/r{t}{sample}"
        if mpath.exists():  # resume: the sealed proposal is reused, no new model call
            m = read_json(mpath)
            if m.get("gate") == "proposal_failed":
                return None
            p = proposer.Proposal(**m["proposal"])
            return Candidate(
                p, m["branch"], m["commit"], m["tree"], m["diff"], m["files"], gate=m.get("gate", "")
            )
        messages = proposer.build_messages(
            domain=self.domain,
            policy_model=self.cfg.policy_model,
            files=files,
            feedback=feedback,
            history=self.history.render(),
            prune=prune,
            memory_block=memory_block,
            budget=budget or 99,
            t=t,
            sample_index=j,
        )
        (rdir / sample).mkdir(parents=True, exist_ok=True)
        (rdir / sample / "prompt.json").write_text(json.dumps(messages, ensure_ascii=False, indent=1))
        seed = self.cfg.seed * 1000 + t * 10 + j
        try:
            text = self.search.ask("proposer", messages, seed=seed)
            (rdir / sample / "response.txt").write_text(text)
            prop = proposer.parse_proposal(text, sample, self.domain, failing)
        except BudgetStop:
            raise
        except (GatewayError, proposer.ProposalError) as exc:
            self.log(f"round {t} {sample}: proposal failed: {exc}")
            write_json(mpath, {"gate": "proposal_failed", "error": str(exc)[:500], "sealed_at": time.time()})
            return None
        wt = self.dir / "wt" / f"r{t}{sample}"
        with self._git_lock:
            self.repo.add_worktree(wt, branch, inc_commit)
            for rel, content in prop.edits.items():
                target = wt / "harness" / rel
                if content is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content)
            msg = f"r{t} {sample}: {prop.root_cause[:60]}\n\n{prop.what[:400]}"
            commit = self.repo.commit_worktree(wt, msg)
            self.repo.remove_worktree(wt)  # the branch keeps the commit; the worktree is not needed
            diff = self.repo.diff(inc_commit, commit)
            changed = self.repo.changed_files(inc_commit, commit)
            tree = self.repo.tree(commit)
        write_json(
            mpath,
            {
                "sealed_at": time.time(),
                "round": t,
                "sample": sample,
                "branch": branch,
                "commit": commit,
                "tree": tree,
                "files": changed,
                "diff": diff,
                "proposal": asdict(prop),
            },
        )
        return Candidate(prop, branch, commit, tree, diff, changed)

    def _llm_screen(
        self, t: int, rdir: Path, live: list[Candidate], memory_block: str
    ) -> dict[str, selector.ScreenVerdict]:
        path = rdir / "screen.json"
        samples = [c.proposal.sample for c in live]
        if path.exists():
            saved = read_json(path)
            return {s: selector.ScreenVerdict(**v) for s, v in saved.items()}
        payload = [
            {
                "sample": c.proposal.sample,
                "root_cause": c.proposal.root_cause,
                "what": c.proposal.what,
                "declared_independent_edits": c.proposal.n_edits,
                "diff": c.diff
                if len(c.diff) <= 40000
                else c.diff[:40000] + "\n[diff truncated by the loop at 40,000 characters]",
            }
            for c in live
        ]
        messages = selector.screen_messages(self.domain.brief, payload, memory_block)
        try:
            text = self.search.ask("selector", messages, seed=self.cfg.seed * 1000 + t, temperature=0.0)
            verdicts = selector.parse_screen(text, samples)
        except BudgetStop:
            raise
        except (GatewayError, ValueError) as exc:
            self.log(f"round {t}: screen failed ({exc}); accepting all")
            verdicts = {
                s: selector.ScreenVerdict(s, True, f"screen unavailable: {exc}"[:200]) for s in samples
            }
        write_json(path, {s: asdict(v) for s, v in verdicts.items()})
        return verdicts

    def _select(
        self, t: int, rdir: Path, inc: EvalResult, admissible: list[Candidate], memory_block: str
    ) -> tuple[Candidate | None, str]:
        path = rdir / "select.json"
        by_sample = {c.proposal.sample: c for c in admissible}
        if path.exists():
            saved = read_json(path)
            return (by_sample.get(saved["choice"]) if saved["choice"] else None), saved["reason"]
        fallback = max(admissible, key=lambda c: (c.ev.score if c.ev else 0.0, -(c.ev.cost if c.ev else 0.0)))
        choice: Candidate | None
        if not self.cfg.use_llm_screen:  # pure-rule configuration: RRSI argmax
            choice, reason = fallback, "argmax over admissible candidates"
        else:
            inc_view = {"evolve_score": round(inc.score * 100, 2), "mean_tokens_per_task": round(inc.cost)}
            cand_view = []
            for c in admissible:
                assert c.ev is not None
                cand_view.append(
                    {
                        "sample": c.proposal.sample,
                        "root_cause": (f"[{c.proposal.cite}] " if c.proposal.cite else "")
                        + c.proposal.root_cause,
                        "what": c.proposal.what,
                        "evolve_score": round(c.ev.score * 100, 2),
                        "mean_tokens_per_task": round(c.ev.cost),
                        "fixed_tasks": sorted(set(c.ev.passed) - set(inc.passed)),
                        "broken_tasks": sorted(set(inc.passed) - set(c.ev.passed)),
                        "predicted": c.proposal.predicted,
                        "files": c.files,
                        "diff_excerpt": c.diff[:3000]
                        + ("\n[excerpt truncated]" if len(c.diff) > 3000 else ""),
                    }
                )
            messages = selector.select_messages(inc_view, cand_view, self.history.render(), memory_block)
            try:
                text = self.search.ask(
                    "selector", messages, seed=self.cfg.seed * 1000 + 500 + t, temperature=0.0
                )
                sample, reason = selector.parse_select(text, list(by_sample))
                choice = by_sample[sample] if sample else None
            except BudgetStop:
                raise
            except (GatewayError, ValueError) as exc:
                choice, reason = fallback, f"selector unavailable ({exc}); argmax fallback"[:300]
        write_json(path, {"choice": choice.proposal.sample if choice else None, "reason": reason})
        return choice, reason

    def _record_sim(self, t: int, c: Candidate, ev: EvalResult) -> None:
        sim = self.sim()
        sample = c.proposal.sample
        if any(
            a.round == t and a.sample == sample and a.domain == self.cfg.provenance
            for p in sim.problems
            for a in p.attempts
        ):
            return  # already recorded (resumed run)
        predicted = c.proposal.predicted
        fixed = sorted(x for x in predicted if ev.outcomes.get(x) and ev.outcomes[x].reward >= 1.0)
        missed = sorted(set(predicted) - set(fixed))
        attempt = Attempt(
            round=t,
            sample=sample,
            accepted=True,
            backfilled=False,
            what=truncate(c.proposal.what),
            files=files_field(c.files),
            predicted=predicted,
            fixed=fixed,
            missed=missed,
            passed=len(ev.passed),
            total=len(ev.outcomes),
            domain=self.cfg.provenance,
        )
        name = c.proposal.root_cause or (c.proposal.cite or "unspecified")
        pid = sim.update(attempt, cite=c.proposal.cite, name=name, opened_round=t)
        sim.save(self.sim_path)
        self.log(f"round {t}: SIM <- {pid} ({len(fixed)}/{len(predicted)} predicted fixes realized)")

    # --------------------------------------------------------------- report
    def summary(self) -> dict[str, Any]:
        path = self.dir / "heldout.jsonl"
        points = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
        start = next((p for p in points if p["after_round"] == -1), None)
        end = max(points, key=lambda p: p["after_round"]) if points else None
        first_tree = start["tree"] if start else ""
        evo_start = self.evaluator.cached(
            self.cfg.evolve_split, self.cfg.policy_model, first_tree, self.cfg.rollout
        )
        evo_end = self.evaluate(EVOLVE, self.cfg.evolve_split)
        recs = self.history.records()
        out = {
            "run_id": self.cfg.run_id,
            "domain": self.cfg.domain,
            "memory": self.cfg.memory,
            "policy_model": self.cfg.policy_model,
            "evolve_split": self.cfg.evolve_split,
            "heldout_split": self.cfg.heldout_split,
            "rounds": self.cfg.rounds,
            "k": self.cfg.k,
            "delta": self.state().get("delta"),
            "evolve_start": evo_start.score if evo_start else None,
            "evolve_end": evo_end.score,
            "heldout_start": start["score"] if start else None,
            "heldout_end": end["score"] if end else None,
            "heldout_curve": points,
            "accepted": sum(r.selected for r in recs),
            "proposed": len(recs),
            "evaluated": sum(r.score is not None for r in recs),
            "screened_out": sum(bool(r.gate) for r in recs),
            "final_commit": self.repo.rev(EVOLVE),
            "sim_entries": len(self.sim().problems),
            "sim_attempts": self.sim().n_attempts,
        }
        write_json(self.dir / "summary.json", out)
        return out
