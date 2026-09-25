"""Core AI inference wrapper for the ported Laya decision model.

Loads an .aimodel asset on one pinned compute unit, mirrors laya's exact
semantics (build_sequence tokenization, temperature calibration, entropy
confidence, act probabilities) so results are drop-in comparable with the
torch reference. One wrapper instance == one event loop == one compute-unit
specialization (verified traps: ANE load failure = SIGABRT not exception;
fresh event loop per call = fd leak).

Run with .venv-infer python (no torch needed).
"""
import asyncio
import json
import os
from pathlib import Path

import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# torch-free tokenization, verbatim-extracted from the audited laya 0.3.5 package
from laya_port.sequence import (QTYPES, QTYPE_NAMES, build_sequence, clamp_temperature,
                                confidence_from_probs, render_options, temp_bucket)

from coreai.runtime import AIModel, NDArray, SpecializationOptions, ComputeUnitKind

UNIT_FACTORIES = {
    "cpu": SpecializationOptions.cpu_only,
    "gpu": lambda: SpecializationOptions.from_preferred_compute_unit_kind(ComputeUnitKind.gpu()),
    "ne": lambda: SpecializationOptions.from_preferred_compute_unit_kind(ComputeUnitKind.neural_engine()),
    "all": SpecializationOptions.default,
}


def _to_np(x):
    return np.asarray(x.numpy() if callable(getattr(x, "numpy", None)) else x.data)


class CoreAIAgent:
    """Blocking front-end over the async coreai runtime; one loop, reused."""

    def __init__(self, aimodel_dir, source_dir, unit="gpu", shape=None, dynamic_L=False):
        # dynamic_L=True -> pad to content length per batch (only valid for assets
        # exported with bounded-dynamic L). Fixed-shape assets MUST pad to full L.
        self.dynamic = dynamic_L
        self.model_dir = str(source_dir)
        self.cfg = json.load(open(os.path.join(self.model_dir, "rl_agent_config.json")))
        self.temperature = [clamp_temperature(t) for t in self.cfg.get("temperature", [1.0] * 3)]
        self.temperature_by_options = {k: clamp_temperature(v)
                                       for k, v in self.cfg.get("temperature_by_options", {}).items()}
        from transformers import AutoTokenizer
        self.tok = AutoTokenizer.from_pretrained(os.path.join(self.model_dir, "tokenizer"))

        self.unit = unit
        self._loop = asyncio.new_event_loop()          # ONE loop for the life of the agent
        opts = UNIT_FACTORIES[unit]()
        self.asset = self._run(AIModel.load(str(aimodel_dir), specialization_options=opts))
        self.fn = self.asset.load_function(self.asset.function_names[0])
        sig = self.fn.signature if hasattr(self.fn, "signature") else None
        # Resolution order: asset-name baked shape (build_aimodel.py bakes
        # b{B}L{L}K{K}; ground truth) > concrete signature dims > caller shape.
        # A stale caller shape (e.g. K=32 against the K128 skill asset) must not
        # survive — it crashes the runtime with 'invalid shape [1,32]'.
        name_shape = self._shape_from_name(aimodel_dir)
        self.shape = name_shape or (shape or self._infer_shape(sig))
        if not self.shape:
            raise RuntimeError("cannot resolve asset shape: signature unavailable and name "
                               "carries no bBLxK tag. Pass shape={'B':..,'L':..,'K':..} explicitly.")
        def _concrete(name, axis):
            try:
                return int({q.name: q for q in sig.inputs}[name].shape[axis])
            except Exception:
                return None
        if not name_shape:
            sB = _concrete("input_ids", 0); sL = _concrete("input_ids", 1)
            sK = _concrete("marker_pos", 1)
            if sB is not None: self.shape["B"] = sB
            if sL is not None: self.shape["L"] = sL
            if sK is not None: self.shape["K"] = sK

    def _run(self, coro_or_val):
        return self._loop.run_until_complete(coro_or_val) if asyncio.iscoroutine(coro_or_val) else coro_or_val

    def _infer_shape(self, sig):
        # shapes come from the function signature: input_ids (B, L), marker_pos (B, K)
        try:
            inps = {p.name: p for p in sig.inputs} if hasattr(sig, "inputs") else {}
            B = inps["input_ids"].shape[0]; L = inps["input_ids"].shape[1]
            K = inps["marker_pos"].shape[1]
            return {"B": int(B), "L": int(L), "K": int(K)}
        except Exception:
            return None

    @staticmethod
    def _shape_from_name(aimodel_dir):
        """Assets bake their shape in the directory name (build_aimodel.py):
        laya[-dyn]-f16-b{B}L{L}K{K}.aimodel. Fallback when the runtime exposes no
        usable signature (observed: fn.signature is None on some builds)."""
        import re as _re
        m = _re.search(r"b(\d+)L(\d+)K(\d+)", os.path.basename(str(aimodel_dir)))
        if not m:
            return None
        return {"B": int(m.group(1)), "L": int(m.group(2)), "K": int(m.group(3))}

    def close(self):
        try:
            self._loop.close()
        except Exception:
            pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ---- fixed-shape collate (mirror of laya.collate_items + pad to export shape) ----
    def _collate(self, items, pad_to=None):
        B, L, K = self.shape["B"], self.shape["L"], self.shape["K"]
        assert len(items) <= B, f"{len(items)} questions > export B={B}"
        if pad_to:                       # bounded-dynamic assets: pad to content, not to L
            L = max(16, min(L, max(len(it["ids"]) for it in items)))
        ids = np.full((B, L), self.tok.pad_token_id, dtype=np.int32)
        att = np.zeros((B, L), dtype=np.int32)
        mpos = np.zeros((B, K), dtype=np.int32)
        mmask = np.zeros((B, K), dtype=bool)
        qt = np.zeros((B,), dtype=np.int32)   # Core AI lowers int64 -> int32 at export
        for i, it in enumerate(items):
            n = len(it["ids"])
            assert n <= L, f"prompt {n} > export L={L} (raise, never truncate)"
            assert len(it["markers"]) <= K, f"{len(it['markers'])} options > export K={K}"
            ids[i, :n] = it["ids"]; att[i, :n] = 1
            k = len(it["markers"])
            mpos[i, :k] = it["markers"]; mmask[i, :k] = True
            qt[i] = it["qtype"]
        return ids, att, mpos, mmask, qt

    def _call(self, arrays):
        ids, att, mpos, mmask, qt = arrays
        out = self._run(self.fn(inputs={
            "input_ids": NDArray(ids), "attention_mask": NDArray(att),
            "marker_pos": NDArray(mpos), "marker_mask": NDArray(mmask),
            "qtype": NDArray(qt)}))
        if asyncio.iscoroutine(out):
            out = self._run(out)
        return _to_np(out["logits"]), _to_np(out["act"])

    def pass_once(self, items_arrays):
        """Timed primitive: one model pass on pre-collated arrays."""
        return self._call(items_arrays)

    # ---- laya-compatible predict ----
    def build_items(self, state, questions):
        items = []
        max_len = min(self.cfg["max_len"], self.shape["L"])
        head_max_len = self.cfg["head_max_len"]
        for qid, qdef in questions.items():
            q = {"t": qdef["type"], "ins": qdef["instructions"], "crit": qdef.get("criteria")}
            if q["t"] == "choice" and isinstance(q["crit"], list):
                q["crit"] = {c: None for c in q["crit"]}
            seq, markers = build_sequence(self.tok, state, q, max_len, head_max_len)
            if len(markers) != len(render_options(q)):
                raise ValueError(f"question {qid!r} options exceed head budget")
            items.append({"qid": qid, "ids": seq, "markers": markers, "qtype": QTYPES[q["t"]], "q": q})
        return items

    def predict(self, state, questions):
        items = self.build_items(state, questions)
        B = self.shape["B"]
        chunks = [items[i:i + B] for i in range(0, len(items), B)]
        answers, acts = {}, []
        for ch in chunks:
            logits, act = self.pass_once(self._collate(ch, pad_to=self.dynamic))
            for r, it in enumerate(ch):
                k = len(it["markers"]); qt = it["qtype"]
                t_scale = self.temperature_by_options.get(temp_bucket(qt, k), self.temperature[qt])
                z = logits[r, :k] / t_scale
                p = np.exp(z - z.max()); p = p / p.sum()
                ext = {"act_probability": round(float(_softmax(act[r])[0]), 4)}
                q = it["q"]
                if q["t"] == "choice":
                    keys = list(q["crit"].keys())
                    answers[it["qid"]] = {"type": "choice", "choice": keys[int(p.argmax())],
                                          "probabilities": {kk: round(float(v), 4) for kk, v in zip(keys, p)},
                                          "confidence": round(confidence_from_probs(p, k), 4), "action": ext}
                elif q["t"] == "score":
                    answers[it["qid"]] = {"type": "score", "score": round(float((np.arange(k) * p).sum()), 4),
                                          "probabilities": {str(i): round(float(v), 4) for i, v in enumerate(p)},
                                          "confidence": round(confidence_from_probs(p, k), 4), "action": ext}
                else:
                    answers[it["qid"]] = {"type": "noul", "noul": round(float(p[1]), 4),
                                          "confidence": round(max(float(p[1]), 1.0 - float(p[1])), 4),
                                          "action": ext}
        return {"answers": answers, "backend": f"coreai-{self.unit}", "shape": self.shape,
                "passes": len(chunks)}


def _softmax(x):
    e = np.exp(np.asarray(x, dtype=np.float64) - np.max(x))
    return e / e.sum()
