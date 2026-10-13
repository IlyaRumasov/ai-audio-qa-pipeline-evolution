#!/usr/bin/env python3
"""Паритет src/qa_core.py <-> src/qa_core.js.

Створює в n8n тимчасовий флоу (Webhook -> Code з ядром JS -> відповідь), активує його,
відправляє ті самі випадки (tests/cases.json + детерміновані випадкові), порівнює з Python
і видаляє флоу. Зовнішніх сервісів, крім вашого n8n, не викликає.

    python tests/run_parity.py --url https://<n8n> --key-file ../.n8n-key.txt
"""
import argparse
import json
import random
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import qa_core as q  # noqa: E402

RUNNER = r"""
const b = $input.first().json.body;
const out = {
  normalize: b.texts.map(t => normalize(t)),
  metrics: b.pairs.map(p => [wer(p[0], p[1]), cer(p[0], p[1])]),
  matching: b.matching.map(c => matchRows(c.rows, c.files)),
  parse: b.parse.map(c => parseResponse(c.resp, c.status)),
  assess: b.assess.map(c => assess(c.ref, c.parsed)),
  prompts: b.prompts.map((p, i) => buildJudgePrompt(p[0], p[1], p[2], i)),
  lines: b.prompts.map((p, i) => [buildJudgeLine(i, p[0], p[1], p[2], 0), buildTranscribeLine('gs://b/x.wav', p[2], 'Latin', 0)]),
  cases: b.prompts.map((p, i) => caseIdOf(buildJudgeLine(i, p[0], p[1], p[2], 0).request)),
  tr: b.parse.map(c => parseTranscription(c.resp, c.status)),
  decide: b.decide.map(c => [decide(c.ref, c.tr), combine(c.tr, c.judge)]),
  report: b.report.map(c => reportRow(c.row, c.match, c.a)),
};
out.summary = summarize(out.report);
out.csv = toCsv(out.report);
return [{ json: out }];
"""

ALPHABET = list("abc xyzABC,.!?-'\"\n\t  ") + ["ශ", "්", "‍", "ර", "வ", "்",
                                             "é", "é", "$", "%", "—", " ", "1", "2"]


def rnd_text(r, n):
    return "".join(r.choice(ALPHABET) for _ in range(r.randint(0, n)))


def rnd_tc(r):
    s = r.randint(0, 50)
    return f"00h{s // 60:02d}m{s % 60:02d}.{r.choice([0, 500]):03d}---00h00m{r.randint(0, 59):02d}.000"


def rnd_matching(r):
    spk = [f"speaker_{i}" for i in range(3)]
    ids = ["7z9hloC1sQp", "5dd4fab2", "aa", "bb"]
    rows, files = [], []
    for _ in range(r.randint(0, 12)):
        rows.append({"file": f"audio/{r.choice(spk)}/{r.choice(ids)}___{rnd_tc(r)}.wav"})
    for k in range(r.randint(0, 12)):
        base = f"{r.choice(ids)}___{rnd_tc(r)}.wav"
        if r.random() < 0.3 and rows:  # частина файлів точно з CSV
            base = rows[r.randrange(len(rows))]["file"].split("/")[-1]
        path = base if r.random() < 0.2 else f"{r.choice(spk)}/{base}"
        files.append({"id": f"f{k}", "path": path})
    return {"rows": rows, "files": files}


def build_payload(seed=7):
    cases = json.loads((ROOT / "tests" / "cases.json").read_text(encoding="utf-8"))
    r = random.Random(seed)
    texts = [c["in"] for c in cases["normalize"]] + [rnd_text(r, 40) for _ in range(300)]
    pairs = [[c["ref"], c["hyp"]] for c in cases["metrics"]] + [[rnd_text(r, 30), rnd_text(r, 30)] for _ in range(300)]
    matching = [{"rows": c["rows"], "files": c["files"]} for c in cases["matching"]] + [rnd_matching(r) for _ in range(200)]
    parse = [{"resp": c["resp"], "status": c.get("status")} for c in cases["parse"]]
    for _ in range(100):
        v = r.choice(["OK", "ok", "MINOR", "BRAK", "brak", "MAYBE", None, 5])
        body = r.choice([
            json.dumps({"verdict": v, "transcription": rnd_text(r, 20), "issues": [rnd_text(r, 8)] * r.randint(0, 7)}),
            f"{v} | {rnd_text(r, 20)}", rnd_text(r, 30), "```json\n" + json.dumps({"verdict": v, "transcription": "t"}) + "\n```"])
        parse.append({"resp": {"candidates": [{"finishReason": r.choice(["STOP", "MAX_TOKENS", "SAFETY", None]),
                                               "content": {"parts": [{"text": body}]}}]}, "status": None})
    assess = [{"ref": rnd_text(r, 30), "parsed": {"verdict": r.choice(["OK", "MINOR", "BRAK", "ERROR"]),
                                                  "transcription": rnd_text(r, 30), "issues": [], "error": ""}} for _ in range(150)]
    prompts = [[rnd_text(r, 50), rnd_text(r, 50), "Tamil"] for _ in range(50)] + [["a 'b' \"c\"\n</x>", "CASE: 9", "Indonesian"]]
    for _ in range(60):  # відповіді кроку 1 (транскрипція)
        obj = {"has_speech": r.choice([True, False, None, "true"]), "transcription": r.choice([rnd_text(r, 20), "", None])}
        obj = {k: v for k, v in obj.items() if v is not None}
        parse.append({"resp": {"candidates": [{"finishReason": r.choice(["STOP", "MAX_TOKENS"]),
                                               "content": {"parts": [{"text": json.dumps(obj)}]}}]}, "status": None})
    decide_cases = []
    for _ in range(150):
        ref, hyp = rnd_text(r, 25), r.choice([None, rnd_text(r, 25), ""])
        hyp = ref if hyp is None else hyp
        tr = {"ok": r.random() > 0.1, "has_speech": r.random() > 0.2, "transcription": hyp,
              "error": r.choice(["", "", "transcription truncated (MAX_TOKENS)"])}
        judge = r.choice([{"verdict": "MINOR", "transcription": "", "issues": ["x"], "error": ""},
                          {"verdict": "ERROR", "transcription": "", "issues": [], "error": "e"}])
        decide_cases.append({"ref": ref, "tr": tr, "judge": judge})
    report = [{"row": {"file": f"audio/speaker_{i % 3}/r___{rnd_tc(r)}.wav", "transcript": rnd_text(r, 20),
                       "speaker": r.choice(["", f"speaker_{i}"])},
               "match": r.choice(["EXACT", "MISSING"]),
               "a": r.choice([None, q.assess("a b", {"verdict": "OK", "transcription": "a c", "issues": ["x, \"y\""], "error": ""})])}
              for i in range(40)]
    return {"texts": texts, "pairs": pairs, "matching": matching, "parse": parse, "assess": assess,
            "prompts": prompts, "report": report, "decide": decide_cases}


def expected(p):
    rep = [q.report_row(c["row"], c["match"], c["a"]) for c in p["report"]]
    return {
        "normalize": [q.normalize(t) for t in p["texts"]],
        "metrics": [[q.wer(a, b), q.cer(a, b)] for a, b in p["pairs"]],
        "matching": [dict(zip(("results", "orphans"), q.match_rows(c["rows"], c["files"]))) for c in p["matching"]],
        "parse": [q.parse_response(c["resp"], c["status"]) for c in p["parse"]],
        "assess": [q.assess(c["ref"], c["parsed"]) for c in p["assess"]],
        "prompts": [q.build_judge_prompt(a, b, c, i) for i, (a, b, c) in enumerate(p["prompts"])],
        "lines": [[q.build_judge_line(i, a, b, c, 0), q.build_transcribe_line("gs://b/x.wav", c, "Latin", 0)] for i, (a, b, c) in enumerate(p["prompts"])],
        "cases": [q.case_id_of(q.build_judge_line(i, a, b, c, 0)["request"]) for i, (a, b, c) in enumerate(p["prompts"])],
        "tr": [q.parse_transcription(c["resp"], c["status"]) for c in p["parse"]],
        "decide": [[q.decide(c["ref"], c["tr"]), q.combine(c["tr"], c["judge"])] for c in p["decide"]],
        "report": rep,
        "summary": q.summarize(rep),
    }


def call(api, key, method, path, body=None):
    req = urllib.request.Request(api + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-N8N-API-KEY": key, "Content-Type": "application/json", "User-Agent": "curl/8.7.1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        t = r.read().decode()
        return json.loads(t) if t else {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--key-file", required=True)
    a = ap.parse_args()
    key = Path(a.key_file).read_text(encoding="utf-8").strip()
    base = a.url.rstrip("/")
    api = base + "/api/v1"
    core = (ROOT / "src" / "qa_core.js").read_text(encoding="utf-8")
    hook = "aiqa-parity-" + uuid.uuid4().hex[:12]
    wf = {
        "name": "[TEST] Audio AI-QA parity (temporary)",
        "nodes": [
            {"id": str(uuid.uuid4()), "name": "Webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2,
             "position": [0, 0], "webhookId": str(uuid.uuid4()),
             "parameters": {"httpMethod": "POST", "path": hook, "responseMode": "lastNode", "options": {}}},
            {"id": str(uuid.uuid4()), "name": "Run core", "type": "n8n-nodes-base.code", "typeVersion": 2,
             "position": [240, 0], "parameters": {"jsCode": core + "\n" + RUNNER}},
        ],
        "connections": {"Webhook": {"main": [[{"node": "Run core", "type": "main", "index": 0}]]}},
        "settings": {"executionOrder": "v1"},
    }
    created = call(api, key, "POST", "/workflows", wf)
    wid = created["id"]
    try:
        call(api, key, "POST", f"/workflows/{wid}/activate")
        time.sleep(2)
        payload = build_payload()
        req = urllib.request.Request(f"{base}/webhook/{hook}", data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "curl/8.7.1"})
        with urllib.request.urlopen(req, timeout=120) as r:
            got = json.loads(r.read().decode())
        exp = expected(payload)
        fails = 0
        for k, ev in exp.items():
            gv = got.get(k)
            if isinstance(ev, list):
                bad = [i for i, (x, y) in enumerate(zip(ev, gv or [])) if x != y]
                if len(ev) != len(gv or []):
                    bad.append("len")
                print(f"{k:10s} {len(ev) - len(bad)}/{len(ev)}")
                for i in bad[:3]:
                    print("   DIFF", k, i, json.dumps(ev[i] if i != "len" else len(ev), ensure_ascii=False)[:300],
                          "|", json.dumps((gv or [None])[i] if i != "len" else len(gv or []), ensure_ascii=False)[:300])
                fails += len(bad)
            else:
                ok = ev == gv
                print(f"{k:10s} {'ok' if ok else 'DIFF'}")
                fails += 0 if ok else 1
        csv_ok = got.get("csv", "").startswith("﻿file,speaker,match")
        print("csv       ", "ok" if csv_ok else "DIFF")
        fails += 0 if csv_ok else 1
        print("RESULT:", "PASS" if fails == 0 else f"FAIL ({fails})")
        return 0 if fails == 0 else 1
    finally:
        try:
            call(api, key, "POST", f"/workflows/{wid}/deactivate")
        finally:
            call(api, key, "DELETE", f"/workflows/{wid}")
            print("temporary workflow deleted:", wid)


if __name__ == "__main__":
    sys.exit(main())
