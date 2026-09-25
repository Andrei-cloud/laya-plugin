"""Offline unit tests for bench/laya_latency.py — canned vectors only, no
daemon, no network. Covers: nearest-rank percentile exactness (even/odd
counts, [1..100] anchors), min/max/count, shape builders (exact question
counts and names: 121 noul rel_i, 32-option choice, 72 labels, 4x30 skill
batches, 4-level score rubric), contract validation, model echo assertion
logic, report builder JSON shape, under_bar vs published Jev bars.
LIVE MODE IS NEVER EXERCISED HERE (parent bench runner owns that)."""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "bench"))

import laya_latency as L  # noqa: E402


# ---------------------------------------------------------------- percentile

class TestPercentileNearestRank:
    def test_one_to_hundred_anchors(self):
        # Nearest-rank definition: [1..100] p50 -> 50, p95 -> 95.
        data = list(range(1, 101))
        assert L.percentile_nearest_rank(data, 50) == 50
        assert L.percentile_nearest_rank(data, 95) == 95

    def test_even_count(self):
        # [10,20,30,40]: p50 rank=ceil(2)=2 -> 20; p95 rank=ceil(3.8)=4 -> 40.
        data = [40, 10, 30, 20]  # unsorted input must not matter
        assert L.percentile_nearest_rank(data, 50) == 20
        assert L.percentile_nearest_rank(data, 95) == 40
        assert L.percentile_nearest_rank(data, 100) == 40

    def test_odd_count(self):
        # [1,2,3,4,5]: p50 rank=ceil(2.5)=3 -> 3; p95 rank=ceil(4.75)=5 -> 5.
        data = [5, 1, 4, 2, 3]
        assert L.percentile_nearest_rank(data, 50) == 3
        assert L.percentile_nearest_rank(data, 95) == 5

    def test_single_sample(self):
        assert L.percentile_nearest_rank([7.5], 50) == 7.5
        assert L.percentile_nearest_rank([7.5], 95) == 7.5
        assert L.percentile_nearest_rank([7.5], 0) == 7.5

    def test_p0_and_p100(self):
        data = [3, 1, 2]
        assert L.percentile_nearest_rank(data, 0) == 1
        assert L.percentile_nearest_rank(data, 100) == 3

    def test_repeats_7_wall_ms_style(self):
        # 7 timing samples: p50 = rank ceil(3.5) = 4th smallest; p95 = top.
        s = [120.0, 98.5, 143.2, 101.1, 110.0, 95.0, 200.0]
        assert L.percentile_nearest_rank(s, 50) == 110.0
        assert L.percentile_nearest_rank(s, 95) == 200.0

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            L.percentile_nearest_rank([], 50)

    def test_out_of_range_raises(self):
        with pytest.raises(ValueError):
            L.percentile_nearest_rank([1, 2], 101)
        with pytest.raises(ValueError):
            L.percentile_nearest_rank([1, 2], -1)


class TestBuildStats:
    def test_min_max_count_p50_p95(self):
        st = L.build_stats([120.0, 98.5, 143.2, 101.1, 110.0, 95.0, 200.0])
        assert st == {"count": 7, "min": 95.0, "max": 200.0,
                      "p50": 110.0, "p95": 200.0}

    def test_keys_exact(self):
        st = L.build_stats([1, 2, 3])
        assert set(st) == {"count", "min", "max", "p50", "p95"}

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            L.build_stats([])


# ---------------------------------------------------------------- builders

class TestGuardrailChoiceShape:
    def test_32_options_exact(self):
        req = L.guardrail_choice_request()
        q = req["questions"]["next_action"]
        assert q["type"] == "choice"
        assert len(q["criteria"]) == 32
        assert "reobserve" in q["criteria"] and "abstain" in q["criteria"]
        assert req["meta"]["n_options"] == 32

    def test_validates_clean(self):
        assert L.validate_request(L.guardrail_choice_request()) == []


class TestRerankNoulShape:
    def test_121_questions_named_rel_i(self):
        req = L.rerank_noul_request(121)
        names = sorted(req["questions"])
        assert len(names) == 121
        # exact name set rel_0..rel_120 (sorted() is lexicographic, so
        # compare the set rather than first/last string positions)
        assert set(names) == {f"rel_{i}" for i in range(121)}

    def test_all_noul_no_criteria(self):
        req = L.rerank_noul_request(121)
        for q in req["questions"].values():
            assert q["type"] == "noul"
            assert "criteria" not in q
            assert q["instructions"].strip()

    def test_single_request_meta(self):
        req = L.rerank_noul_request(121)
        assert req["meta"]["n_questions"] == 121
        assert req["meta"]["bar_scope"] == "full rerank round (121 passages)"

    def test_validates_clean(self):
        assert L.validate_request(L.rerank_noul_request(121)) == []


class TestSkillpickShape:
    def test_4_requests_of_30(self):
        reqs = L.skillpick_choice_requests(120, 30)
        assert len(reqs) == 4
        assert [len(r["questions"]) for r in reqs] == [30, 30, 30, 30]

    def test_question_names_sequential_across_batches(self):
        reqs = L.skillpick_choice_requests(120, 30)
        names = [n for r in reqs for n in r["questions"]]
        assert names == [f"S{i}" for i in range(120)]

    def test_every_question_choice_with_none_option(self):
        reqs = L.skillpick_choice_requests(120, 30)
        for r in reqs:
            for q in r["questions"].values():
                assert q["type"] == "choice"
                assert "none" in q["criteria"]

    def test_validates_clean(self):
        for r in L.skillpick_choice_requests(120, 30):
            assert L.validate_request(r) == []

    def test_indivisible_raises(self):
        with pytest.raises(ValueError):
            L.skillpick_choice_requests(120, 32)


class TestRouteScoreShape:
    def test_4_level_rubric_lowest_first(self):
        req = L.route_score_request()
        q = req["questions"]["difficulty"]
        assert q["type"] == "score"
        assert len(q["criteria"]) == 4
        assert q["criteria"][0] == "Trivial"
        assert q["criteria"][-1] == "Expert"

    def test_validates_clean(self):
        assert L.validate_request(L.route_score_request()) == []


class TestBtzscShape:
    def test_72_labels_exact_unique(self):
        labels = L.btzsc_labels()
        assert len(labels) == 72
        assert len(set(labels)) == 72

    def test_choice_question_over_all_72(self):
        req = L.btzsc_choice_request()
        q = req["questions"]["label"]
        assert q["type"] == "choice"
        assert set(q["criteria"]) == set(L.btzsc_labels())
        assert req["meta"]["n_options"] == 72

    def test_validates_clean(self):
        assert L.validate_request(L.btzsc_choice_request()) == []


class TestBuildRequests:
    def test_five_shapes_in_order(self):
        shapes = dict(L.build_requests())
        assert list(shapes) == ["guardrail-choice-32", "rerank-noul-121",
                                "skillpick-choice-120", "route-score-4",
                                "btzsc-choice-72"]

    def test_request_counts_per_shape(self):
        counts = {name: len(reqs) for name, reqs in L.build_requests()}
        assert counts == {"guardrail-choice-32": 1, "rerank-noul-121": 1,
                          "skillpick-choice-120": 4, "route-score-4": 1,
                          "btzsc-choice-72": 1}


# ---------------------------------------------------------------- validation

class TestValidateRequest:
    def test_state_over_60k_chars_flagged(self):
        req = L.rerank_noul_request(121)
        req["state"] = "x" * 70_000
        v = L.validate_request(req)
        assert any("state_too_large" in s for s in v)

    def test_unknown_type_flagged(self):
        req = {"state": "s", "questions": {"a": {"type": "binary",
                                                 "instructions": "x"}},
               "meta": {"n_options": 0, "n_questions": 1, "bar_scope": "t"}}
        assert any("unknown question type" in s for s in L.validate_request(req))

    def test_noul_with_criteria_flagged(self):
        req = {"state": "s",
               "questions": {"a": {"type": "noul", "instructions": "x",
                                   "criteria": {"y": "z"}}},
               "meta": {"n_options": 0, "n_questions": 1, "bar_scope": "t"}}
        assert any("criteria" in s for s in L.validate_request(req))

    def test_choice_one_option_flagged(self):
        req = {"state": "s",
               "questions": {"a": {"type": "choice", "instructions": "x",
                                   "criteria": {"only": "one"}}},
               "meta": {"n_options": 1, "n_questions": 1, "bar_scope": "t"}}
        assert any(">=2 criteria" in s for s in L.validate_request(req))

    def test_score_short_rubric_flagged(self):
        req = {"state": "s",
               "questions": {"a": {"type": "score", "instructions": "x",
                                   "criteria": ["only"]}},
               "meta": {"n_options": 1, "n_questions": 1, "bar_scope": "t"}}
        assert any("rubric list" in s for s in L.validate_request(req))

    def test_empty_instructions_flagged(self):
        req = {"state": "s",
               "questions": {"a": {"type": "noul", "instructions": "  "}},
               "meta": {"n_options": 0, "n_questions": 1, "bar_scope": "t"}}
        assert any("instructions" in s for s in L.validate_request(req))


# ---------------------------------------------------------------- model echo

class TestModelEcho:
    def test_matching_echo_passes(self):
        observed, ok = L.check_model_echo({"model": "laya-combined-f16",
                                           "answers": {}},
                                          "laya-combined-f16")
        assert ok is True
        assert observed == "laya-combined-f16"

    def test_mismatched_echo_fails(self):
        observed, ok = L.check_model_echo({"model": "something-else"},
                                          "laya-combined-f16")
        assert ok is False
        assert observed == "something-else"

    def test_absent_echo_passes_but_observed_none(self):
        observed, ok = L.check_model_echo({"answers": {}}, "laya-combined-f16")
        assert ok is True          # absence is inconclusive-pass, not failure
        assert observed is None

    def test_non_dict_response(self):
        observed, ok = L.check_model_echo(None, "m")
        assert (observed, ok) == (None, True)


# ---------------------------------------------------------------- report

class TestReportShape:
    def _items(self):
        reqs = [L.route_score_request()]
        a = L.build_report_item("route-score-4", [100.0, 110.0, 120.0, 90.0,
                                                  130.0, 95.0, 200.0],
                                reqs, "laya-combined-f16", echoes_ok=True)
        err_reqs = [L.guardrail_choice_request()]
        b = L.build_report_item("guardrail-choice-32", [], err_reqs,
                                "laya-combined-f16", echoes_ok=False,
                                error="URLError: boom")
        return [a, b]

    def test_report_item_stats_and_meta(self):
        a = self._items()[0]
        assert a["shape"] == "route-score-4"
        assert a["stats"] == {"count": 7, "min": 90.0, "max": 200.0,
                              "p50": 110.0, "p95": 200.0}
        assert a["requests"] == 1
        assert a["questions_per_request"] == [1]
        assert a["options_max"] == 4
        assert a["echoes_ok"] is True
        assert "error" not in a

    def test_report_item_error_shape(self):
        b = self._items()[1]
        assert b["stats"] is None
        assert b["error"] == "URLError: boom"
        assert b["echoes_ok"] is False

    def test_full_report_json_shape_sorted(self):
        rep = L.build_report("http://127.0.0.1:11270", "laya-combined-f16", 7,
                             self._items(), generated_at="2026-09-25T00:00:00+00:00")
        assert rep["schema"] == L.REPORT_SCHEMA
        assert set(rep) == {"schema", "generated_at", "base_url", "model",
                            "repeats", "shapes", "overall"}
        assert rep["overall"] == {"shapes_ok": 1, "shapes_total": 2}
        assert len(rep["shapes"]) == 2
        # pretty + sorted keys round-trip
        text = json.dumps(rep, indent=2, sort_keys=True)
        assert json.loads(text) == rep
        # sorted keys visible in serialization order
        assert text.index('"base_url"') < text.index('"generated_at"') \
            < text.index('"model"')

    def test_under_bar_booleans_vs_published_bars(self):
        # route p50 = 110 ms -> under 0.4 s bar. choose shape: no stats -> None.
        items = self._items()
        stats = {"route-score-4": {"stats": items[0]["stats"]},
                 "guardrail-choice-32": {"stats": None}}
        ub = L.apply_bars(stats, dict(L.DEFAULT_BARS))
        assert ub["route-score-4"] is True          # 0.110 s < 0.4 s
        assert ub["guardrail-choice-32"] is None    # errored shape

    def test_under_bar_over_bar(self):
        stats = {"route-score-4": {"stats": {"p50": 450.0, "min": 400.0,
                                             "max": 500.0, "p95": 500.0,
                                             "count": 7}}}
        ub = L.apply_bars(stats, dict(L.DEFAULT_BARS))
        assert ub["route-score-4"] is False         # 0.45 s > 0.4 s

    def test_under_bar_rerank_boundary(self):
        # 121-question round: exactly-at-bar is NOT under (strict <).
        stats = {"rerank-noul-121": {"stats": {"p50": 1900.0, "min": 1.0,
                                               "max": 1.0, "p95": 1.0,
                                               "count": 7}}}
        ub = L.apply_bars(stats, dict(L.DEFAULT_BARS))
        assert ub["rerank-noul-121"] is False

    def test_report_with_under_bar_includes_bars(self):
        ub = {"route-score-4": True}
        rep = L.build_report("u", "m", 7, self._items(), under_bar=ub,
                             generated_at="t")
        assert rep["under_bar"] == ub
        assert rep["bars"] == L.DEFAULT_BARS

    def test_no_bars_no_under_bar_keys(self):
        rep = L.build_report("u", "m", 7, self._items(), generated_at="t")
        assert "under_bar" not in rep and "bars" not in rep


# ---------------------------------------------------------------- cli glue

class TestCliDefaults:
    def test_argparse_defaults(self):
        a = L.parse_args([])
        assert a.base_url == "http://127.0.0.1:11270"
        assert a.repeats == 7
        assert a.out == "bench/runs/laya_latency.json"

    def test_argparse_overrides(self):
        a = L.parse_args(["--base-url", "http://x:1/", "--repeats", "3",
                          "--out", "/tmp/r.json", "--bars", "b.json"])
        assert (a.base_url, a.repeats, a.out, a.bars) == \
            ("http://x:1/", 3, "/tmp/r.json", "b.json")

    def test_load_bars_file_override(self, tmp_path):
        f = tmp_path / "bars.json"
        f.write_text(json.dumps({"route": 0.25, "custom-bar": 9.9}))
        bars = L.load_bars(str(f))
        assert bars["route"] == 0.25           # overridden
        assert bars["rerank-round"] == 1.9     # published default kept
        assert bars["custom-bar"] == 9.9

    def test_load_bars_none_gives_published(self):
        assert L.load_bars(None) == {"route": 0.4, "rerank-round": 1.9,
                                     "choose": 0.5}

    def test_shape_bar_keys_cover_all_shapes(self):
        assert set(L.SHAPE_BAR_KEYS) == {name for name, _ in L.build_requests()}


# ---------------------------------------------------------------- live wiring
# main()/run_shape() wiring exercised OFFLINE with a stubbed transport — this
# is the regression lock for the undefined-name crash in main() that shipped
# once (tests were green because main() had no test).

def _stub_transport(monkeypatch, model="laya-combined-f16", fail=False):
    """Replace post_json: record URLs, return a canned {answers, model} body."""
    calls = []

    def fake_post(base_url, payload, timeout=None):
        url = base_url.rstrip("/") + L.QUESTION_API_PATH
        calls.append(url)
        if fail:
            raise OSError("stub network down")
        answers = {n: {"type": "noul", "noul": 0.5}
                   for n in payload["questions"]}
        return {"answers": answers, "model": payload.get("model", model),
                "usage": {"requests": 1}}, 12.5
    monkeypatch.setattr(L, "post_json", fake_post)
    return calls


class TestMainWiring:
    def test_main_offline_end_to_end(self, monkeypatch, tmp_path):
        calls = _stub_transport(monkeypatch)
        out = tmp_path / "rep.json"
        rc = L.main(["--out", str(out), "--repeats", "2"])
        assert rc == 0
        rep = json.loads(out.read_text())
        assert rep["schema"] == L.REPORT_SCHEMA
        assert rep["overall"]["shapes_total"] == 5
        assert rep["overall"]["shapes_ok"] == 5
        # every call went to the §1 wire path, never a bare "/"
        assert all(u.endswith("/v1/systemone") for u in calls)
        assert rep["under_bar"]["rerank-noul-121"] is True  # 12.5ms < 1.9s
        assert all(i["stats"] for i in rep["shapes"])

    def test_main_honours_bars_file(self, monkeypatch, tmp_path):
        _stub_transport(monkeypatch)
        bars = tmp_path / "bars.json"
        bars.write_text(json.dumps({"rerank-round": 0.005}))  # 5 ms bar
        out = tmp_path / "rep.json"
        L.main(["--out", str(out), "--repeats", "1", "--bars", str(bars)])
        rep = json.loads(out.read_text())
        assert rep["under_bar"]["rerank-noul-121"] is False  # 12.5ms > 5ms

    def test_run_shape_network_error_degrades_cleanly(self, monkeypatch):
        _stub_transport(monkeypatch, fail=True)
        reqs = [L.guardrail_choice_request()]
        item, samples = L.run_shape("http://x:1", "guardrail-choice-32",
                                    reqs, "laya-combined-f16", 2)
        assert samples is None
        assert item["stats"] is None
        assert "stub network down" in item["error"]

    def test_run_shape_echo_mismatch_fails_shape(self, monkeypatch):
        def wrong_echo(base_url, payload, timeout=None):
            return {"answers": {}, "model": "someone-elses-model"}, 9.0
        monkeypatch.setattr(L, "post_json", wrong_echo)
        item, samples = L.run_shape(
            "http://x:1", "route-score-4", [L.route_score_request()],
            "laya-combined-f16", 1)
        assert samples and item["echoes_ok"] is False

    def test_post_json_targets_question_api_path(self):
        # URL construction lock (transport never opened): the wire path from
        # §1 must be used verbatim — interop constant, not ours to rename.
        assert L.QUESTION_API_PATH == "/v1/systemone"
        captured = {}
        real_req = L.urllib.request.Request

        class Spy(real_req):
            def __init__(self, url, *a, **k):
                captured["url"] = url
                super().__init__(url, *a, **k)
        L.urllib.request.Request = Spy
        try:
            def fake_urlopen(req, timeout=None):
                raise OSError("stop before send")
            real_open = L.urllib.request.urlopen
            L.urllib.request.urlopen = fake_urlopen
            try:
                L.post_json("http://127.0.0.1:9/", {"state": "s",
                          "model": "m", "questions": {"q": {"type": "noul",
                          "instructions": "x?"}}})
            except OSError:
                pass
            finally:
                L.urllib.request.urlopen = real_open
        finally:
            L.urllib.request.Request = real_req
        assert captured["url"] == "http://127.0.0.1:9/v1/systemone"
