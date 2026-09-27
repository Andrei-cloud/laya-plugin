#!/usr/bin/env python
"""Combined Core AI deployment model: ONE shared frozen encoder + N trained
head-chains, selected per sample by `head_idx`.

Fact that makes this legal: every specialist head fine-tuned the same frozen
encoder — the FT checkpoints' encoder tensors are BIT-IDENTICAL to the source
checkpoint (asserted at build time, all heads). So encoding once and running
each head-chain on the shared hidden state reproduces each specialist exactly.

Per-head chain mirrors laya.common.DecisionModel.forward after the encoder:
    h_i = h + type_emb_i(qtype)
    h_i = TransformerEncoder_i(h_i, pad)
    m_i = gather(h_i, marker_pos)
    logits_i = scorer_i(m_i).masked_fill(~marker_mask, -1e4)
    act_i    = act_head_i([h_i[:,0], top1, gap, ent, k/255])
Selection: torch.where(head_idx == i) per sample, so a batch may mix tasks.

Outputs (logits (B,K), act (B,2)) are the raw un-tempered values; deployment
temperatures are per-head metadata applied by the wrapper (provenance JSON),
exactly as laya.agent applies temperature_by_options.
"""
import torch
import torch.nn as nn

HEADS_ORDER = ["base", "ft_lang", "ft_gr14_ppo", "ft_tool5v2_ppo", "ft_skill4_ppo",
               "ft_triage5", "ft_mail8", "ft_sup3", "ppo_cho", "ft_cmp2", "ft_rrk3"]
# chain 0 stays base (foreign-slate rule, r31). v2 adds the three new-family
# champions as chains 8-10 (pack-gate winners, docs/FINETUNE.md r32-r35):
#   ppo_cho  choose 0.650 (PPO-on-acted, r32)
#   ft_cmp2  compact 3/10 vs base 0/10 (r35; teacher ceiling 0/10 — pack
#            measures the confident-keep side, target was "beat base")
#   ft_rrk3  rerank 1/10 exact vs base 0/10 (r35 after referent+split fixes)
# triage moves 0 -> 5 (ft_triage5 pack champion 0.625 vs base 0.375).
TASK_ROUTE = {"triage": 5, "lang_route": 1, "guardrail": 2, "act_escalate": 2,
              "tool_route": 3, "skill_route": 4,
              "mail_sort": 6, "supervise": 7,
              "choose": 8, "compact": 9, "rerank": 10}


class CombinedDecisionModel(nn.Module):
    """One encoder, len(HEADS) head-chains. forward(ids, att, mpos, mmask, qtype, head_idx)."""

    def __init__(self, encoder, head_spec, chains):
        super().__init__()
        self.encoder = encoder
        d = encoder.config.hidden_size
        self.chains = nn.ModuleList()
        for _ in chains:
            emb = nn.Embedding(3, d)
            layer = nn.TransformerEncoderLayer(d, max(1, d // 64), 4 * d, 0.1,
                                               batch_first=True, norm_first=True)
            head = nn.TransformerEncoder(layer, head_spec, enable_nested_tensor=False)
            scorer = nn.Sequential(nn.LayerNorm(d), nn.Linear(d, d), nn.GELU(), nn.Linear(d, 1))
            act = nn.Sequential(nn.Linear(d + 4, 256), nn.GELU(), nn.Linear(256, 2))
            self.chains.append(nn.ModuleDict(dict(type_emb=emb, head=head,
                                                  scorer=scorer, act_head=act)))

    def load_chains(self, state_dicts):
        """state_dicts: list aligned with HEADS_ORDER; base = source model.safetensors."""
        for i, sd in enumerate(state_dicts):
            sub = {}
            for k, v in sd.items():
                if k.startswith("type_emb."):
                    sub[k] = v
                elif k.startswith("head.") or k.startswith("scorer.") or k.startswith("act_head."):
                    sub[k] = v
                elif k.startswith("temperature"):
                    continue          # wrapper-side metadata, not graph weights
            missing = [k for k in list(self.chains[i].state_dict()) if k not in sub]
            if missing:
                raise RuntimeError(f"chain {i}: missing tensors {missing[:4]}")
            self.chains[i].load_state_dict(sub, strict=True)

    @staticmethod
    def _chain(chain, h, attention_mask, qtype, marker_pos, marker_mask):
        h = h + chain["type_emb"](qtype)[:, None, :]
        pad = ~attention_mask.bool()
        for layer in chain["head"].layers:
            h = layer(h, src_key_padding_mask=pad)
        idx = marker_pos.clamp(min=0)[:, :, None].expand(-1, -1, h.size(-1))
        m = torch.gather(h, 1, idx)
        logits = chain["scorer"](m).squeeze(-1).float()
        logits = logits.masked_fill(~marker_mask, -1e4)
        p = torch.softmax(logits, -1)
        k = marker_mask.sum(-1).clamp(min=2).float()
        ent = -(p * torch.log(p.clamp_min(1e-9))).sum(-1) / torch.log(k)
        top1 = p.topk(1, -1).values
        top2 = p.topk(2, -1).values if p.size(-1) >= 2 else \
            torch.cat([top1, torch.zeros_like(top1)], dim=-1)
        feats = torch.stack([top2[:, 0], top2[:, 0] - top2[:, 1], ent, k / 255.0], -1)
        act = chain["act_head"](torch.cat([h[:, 0].float(), feats], -1))
        return logits, act

    def forward(self, input_ids, attention_mask, marker_pos, marker_mask, qtype, head_idx):
        h = self.encoder(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
        sel = head_idx[:, None]
        logits_out = None
        act_out = None
        for i, chain in enumerate(self.chains):
            lg, ac = self._chain(chain, h, attention_mask, qtype, marker_pos, marker_mask)
            m = (sel == i)
            if logits_out is None:
                logits_out, act_out = torch.where(m, lg, lg), torch.where(m, ac, ac)
            else:
                logits_out = torch.where(m, lg, logits_out)
                act_out = torch.where(m, ac, act_out)
        return logits_out, act_out
