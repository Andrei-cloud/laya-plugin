"""Combined-head Core AI deployment wrapper.

ONE .aimodel asset (shared encoder + 5 head-chains selected by `head_idx`)
serving all routed tasks: triage->base, lang_route->chain 1, guardrail &
act/escalate->chain 2, tool_route->chain 3, skill_route->chain 4.

Semantics mirror laya exactly: build_sequence tokenization, per-head
temperature_by_options calibration (each head has its own fitted temps),
entropy confidence, act probabilities — results are drop-in comparable with
the torch reference and the per-head CoreAIAgent.

Run with the torch-free inference venv (coreai-core + transformers only).
"""
import asyncio, json, os, sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from laya_port.sequence import (QTYPES, build_sequence, clamp_temperature,
                                confidence_from_probs, temp_bucket)
from coreai.runtime import AIModel, NDArray, SpecializationOptions, ComputeUnitKind

UNIT_FACTORIES = {
    "cpu": SpecializationOptions.cpu_only,
    "gpu": lambda: SpecializationOptions.from_preferred_compute_unit_kind(ComputeUnitKind.gpu()),
    "ne": lambda: SpecializationOptions.from_preferred_compute_unit_kind(ComputeUnitKind.neural_engine()),
}
DEFAULT_ROUTE = {"triage": 0, "lang_route": 1, "guardrail": 2, "act_escalate": 2,
                 "tool_route": 3, "skill_route": 4}


def _to_np(x):
    return np.asarray(x.numpy() if callable(getattr(x, "numpy", None)) else x.data)


class CombinedAgent:
    """Blocking front-end; one event loop, one pinned compute unit per instance."""

    def __init__(self, asset_dir, source_dir, unit="gpu"):
        self.asset_dir = str(asset_dir)
        prov_path = os.path.join(os.path.dirname(self.asset_dir.rstrip("/")),
                                 "combined_provenance.json")
        self.prov = json.load(open(prov_path))
        self.L = self.prov["shape"]["L_max"]; self.K = self.prov["shape"]["K_max"]
        self.B = self.prov["shape"]["B"]
        self.chain_order = list(self.prov["heads"])          # HEADS_ORDER
        self.route = dict(DEFAULT_ROUTE)
        self.source_dir = str(source_dir)
        self.cfg = json.load(open(os.path.join(source_dir, "rl_agent_config.json")))
        # per-chain temperatures, from each head's own rl_agent_config.json
        self.temps = []
        for h in self.chain_order:
            hp = os.path.join(os.path.dirname(source_dir), h, "rl_agent_config.json")
            t = {"temperature": [1.0] * 3, "temperature_by_options": {}}
            if h != "base" and os.path.exists(hp):
                t = json.load(open(hp))
            self.temps.append({
                "temperature": [clamp_temperature(x) for x in t.get("temperature", [1.0] * 3)],
                "temperature_by_options": {k: clamp_temperature(v) for k, v
                                           in t.get("temperature_by_options", {}).items()}})
        from transformers import AutoTokenizer
        self.tok = AutoTokenizer.from_pretrained(os.path.join(source_dir, "tokenizer"))
        self._loop = asyncio.new_event_loop()
        opts = UNIT_FACTORIES[unit]()
        self.asset = self._run(AIModel.load(self.asset_dir, specialization_options=opts))
        self.fn = self.asset.load_function(self.asset.function_names[0])

    def _run(self, coro_or_val):
        return self._loop.run_until_complete(coro_or_val) if asyncio.iscoroutine(coro_or_val) else coro_or_val

    def close(self):
        try:
            self._loop.close()
        except Exception:
            pass

    def _encode(self, task, state, question):
        chain = self.route[task]
        qname = list(question)[0]
        q = question[qname]
        crit = q.get("criteria") or {}
        if crit:
            qb = {"t": "choice", "ins": q.get("instructions", ""),
                  "crit": {k: str(v)[:120] for k, v in crit.items()}}
            qt = QTYPES["choice"]
        else:
            qb = {"t": "noul", "ins": q.get("instructions", ""), "crit": {"yes": "yes", "no": "no"}}
            qt = QTYPES["noul"]
        st = state if isinstance(state, str) else json.dumps(state, sort_keys=True)[:900]
        seq, markers = build_sequence(self.tok, st, qb, self.L, 256)
        assert len(markers) <= self.K, f"{len(markers)} options > asset K={self.K}"
        return chain, qb, qt, seq, markers

    def decide(self, task, state, question, pad_to=None):
        """One decision: routed chain, calibrated probabilities, confidence, act.
        pad_to: fix the sequence length (graph-shape cache — Core AI
        re-specializes ~3-5 s per distinct input SHAPE; a serving harness
        should pad every call to asset L_max once and pay it at startup —
        measured: 5 s startup + ~16 ms/call warm vs 3.4 s per variable call).
        Padded positions are mask=0: semantics identical (attention-masked)."""
        chain, qb, qt, seq, markers = self._encode(task, state, question)
        B, L, K = 1, max(16, len(seq)), self.K
        if pad_to:
            L = max(L, pad_to)
        ids = np.full((B, L), self.tok.pad_token_id, dtype=np.int32)
        att = np.zeros((B, L), dtype=np.int32)
        mpos = np.zeros((B, K), dtype=np.int32)
        mmask = np.zeros((B, K), dtype=bool)
        ids[0, :len(seq)] = seq; att[0, :len(seq)] = 1
        mpos[0, :len(markers)] = markers; mmask[0, :len(markers)] = True
        qt_a = np.full((B,), qt, dtype=np.int32)
        hi = np.full((B,), chain, dtype=np.int32)
        out = self._run(self.fn(inputs={
            "input_ids": NDArray(np.ascontiguousarray(ids)),
            "attention_mask": NDArray(np.ascontiguousarray(att)),
            "marker_pos": NDArray(np.ascontiguousarray(mpos)),
            "marker_mask": NDArray(np.ascontiguousarray(mmask)),
            "qtype": NDArray(qt_a), "head_idx": NDArray(hi)}))
        if asyncio.iscoroutine(out):
            out = self._run(out)
        logits = _to_np(out["logits"])[0][: len(markers)]
        act = _to_np(out["act"])[0]
        tp = self.temps[chain]
        t = tp["temperature_by_options"].get(temp_bucket(qt, len(markers)),
                                             tp["temperature"][qt])
        logits = logits / max(t, 1e-6)
        p = np.exp(logits - logits.max()); p /= p.sum()
        names = list(qb["crit"]) if qb["t"] == "choice" else ["yes", "no"]
        best = int(p.argmax())
        conf = float(confidence_from_probs(p, len(p)))
        act_p = float(np.exp(act - act.max())[1] / np.exp(act - act.max()).sum())
        return {"task": task, "chain": self.chain_order[chain],
                "choice": names[best] if qb["t"] == "choice" else ("yes" if best == 1 else "no"),
                "confidence": conf, "probs": {n: float(x) for n, x in zip(names, p)},
                "act_p": act_p, "acted": conf >= 0.7}
