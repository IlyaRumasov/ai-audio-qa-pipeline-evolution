#!/usr/bin/env python3
"""Наскрізні тести флоу A і B у n8n на заглушках (без Drive, Sheets, Gemini, GCS, Vertex, Gmail).

Для кожного сценарію: тестова копія флоу (ручний тригер -> Webhook, «Налаштування» -> Code з
параметрами сценарію, зовнішні вузли -> Code-заглушки з готовими відповідями, Wait -> пропуск),
імпорт, активація, запит, читання даних виконання через API, перевірки, видалення копії.

    python tests/run_flow_tests.py --url https://<n8n> --key-file ../.n8n-key.txt
"""
import argparse
import copy
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
import qa_core as q  # noqa: E402
import build  # noqa: E402

EXTERNAL = {"n8n-nodes-base.googleDrive", "n8n-nodes-base.googleSheets", "n8n-nodes-base.httpRequest",
            "n8n-nodes-base.gmail", "n8n-nodes-base.wait", "n8n-nodes-base.executeWorkflow"}
KEEP_FLAGS = ("executeOnce", "alwaysOutputData", "onError")

STUB = r"""
const S = (($('Webhook').first().json.body.stubs) || {})[__NAME__];
const inp = $input.all();
const run = $runIndex;
const b64 = (t) => Buffer.from(String(t), 'utf8').toString('base64');
if (S === undefined) return inp.map(i => ({ json: i.json }));
switch (S.type) {
  case 'items': return S.items.map(j => ({ json: j }));
  case 'sequence': return S.seq[Math.min(run, S.seq.length - 1)].map(j => ({ json: j }));
  case 'binary': return [{ json: {}, binary: { data: { data: b64(S.text), mimeType: S.mime || 'text/csv', fileName: S.fileName || 'dataset.csv' } } }];
  case 'perItem': {
    const out = [];
    inp.forEach((i, k) => (S.map[String(i.json[S.key])] || []).forEach(j => out.push({ json: j, pairedItem: { item: k } })));
    return out;
  }
  case 'audio': return inp.map(i => {
    if ((S.fail || []).includes(i.json._file_id)) throw new Error('stub: download failed ' + i.json._file_id);
    return { json: { id: i.json._file_id }, binary: { data: { data: b64('RIFF-stub-' + i.json._file_id), mimeType: 'audio/wav', fileName: 'a.wav' } } };
  });
  case 'byField': return inp.map(i => {
    // значення поля вхідного елемента за шляхом 'a.b.c' -> готова відповідь
    const v = S.path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), i.json);
    const r = S.map[String(v)];
    return { json: r === undefined ? S.default : r };
  });
  case 'byName': return inp.map(i => ({ json: { data: S.files[i.json.name] || '' } }));
  case 'text': return [{ json: { data: S.text } }];
  case 'echo': return inp.map(i => ({ json: Object.assign({ stub: __NAME__ }, i.json) }));
  default: throw new Error('unknown stub type ' + S.type);
}
"""

CFG_JS = r"""
const defaults = __DEFAULTS__;
const over = ($('Webhook').first().json.body.cfg) || {};
return [{ json: Object.assign({}, defaults, { run_id: 'test-run' }, over) }];
"""


class Api:
    def __init__(self, url, key):
        self.base, self.key = url.rstrip("/"), key

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + "/api/v1" + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-N8N-API-KEY": self.key, "Content-Type": "application/json",
                                              "User-Agent": "curl/8.7.1"})
        with urllib.request.urlopen(req, timeout=120) as r:
            t = r.read().decode()
            return json.loads(t) if t else {}

    def hook(self, path, payload):
        req = urllib.request.Request(self.base + "/webhook/" + path, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "curl/8.7.1"})
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()


def harness(wf, hook):
    w = copy.deepcopy(wf)
    w["name"] = "[TEST-HARNESS] " + w["name"]
    w["settings"] = {"executionOrder": "v1", "saveManualExecutions": True,
                     "saveDataSuccessExecution": "all", "saveDataErrorExecution": "all"}
    nodes = []
    for n in w["nodes"]:
        if n["type"] == "n8n-nodes-base.manualTrigger":
            nodes.append({"id": n["id"], "name": "Webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2,
                          "position": n["position"], "webhookId": str(uuid.uuid4()),
                          "parameters": {"httpMethod": "POST", "path": hook, "responseMode": "onReceived", "options": {}}})
            w["connections"]["Webhook"] = w["connections"].pop(n["name"])
            continue
        if n["type"] == "n8n-nodes-base.executeWorkflowTrigger":
            # підфлоу: Webhook -> Code з тим самим ім'ям, що повертає вхідні параметри з тіла запиту
            nodes.append({"id": str(uuid.uuid4()), "name": "Webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2,
                          "position": [n["position"][0] - 220, n["position"][1]], "webhookId": str(uuid.uuid4()),
                          "parameters": {"httpMethod": "POST", "path": hook, "responseMode": "onReceived", "options": {}}})
            nodes.append({"id": n["id"], "name": n["name"], "type": "n8n-nodes-base.code", "typeVersion": 2,
                          "position": n["position"], "parameters": {"jsCode": "return [{ json: $('Webhook').first().json.body.input }];"}})
            w["connections"]["Webhook"] = {"main": [[{"node": n["name"], "type": "main", "index": 0}]]}
            continue
        if n["name"] == "Налаштування":
            defaults = {a["name"]: a["value"] for a in n["parameters"]["assignments"]["assignments"]
                        if not str(a["value"]).startswith("=")}
            nodes.append({"id": n["id"], "name": n["name"], "type": "n8n-nodes-base.code", "typeVersion": 2,
                          "position": n["position"],
                          "parameters": {"jsCode": CFG_JS.replace("__DEFAULTS__", json.dumps(defaults, ensure_ascii=False))}})
            continue
        if n["type"] in EXTERNAL:
            s = {"id": n["id"], "name": n["name"], "type": "n8n-nodes-base.code", "typeVersion": 2,
                 "position": n["position"], "parameters": {"jsCode": STUB.replace("__NAME__", json.dumps(n["name"], ensure_ascii=False))}}
            for k in KEEP_FLAGS:
                if k in n:
                    s[k] = n[k]
            if n["type"] == "n8n-nodes-base.httpRequest" and n.get("onError") == "continueRegularOutput":
                s.pop("onError", None)  # помилки HTTP заглушка повертає як {error: ...}
            nodes.append(s)
            continue
        nodes.append(n)
    w["nodes"] = nodes
    return {k: w[k] for k in ("name", "nodes", "connections", "settings")}


def run_case(api, wf, payload, expect_status="success"):
    hook = "aiqa-test-" + uuid.uuid4().hex[:10]
    created = api.call("POST", "/workflows", harness(wf, hook))
    wid = created["id"]
    try:
        api.call("POST", f"/workflows/{wid}/activate")
        time.sleep(1.5)
        api.hook(hook, payload)
        for _ in range(60):
            time.sleep(2)
            ex = api.call("GET", f"/executions?workflowId={wid}&limit=1")["data"]
            if ex and ex[0].get("stoppedAt"):
                break
        full = api.call("GET", f"/executions/{ex[0]['id']}?includeData=true")
        return full
    finally:
        try:
            api.call("POST", f"/workflows/{wid}/deactivate")
        finally:
            api.call("DELETE", f"/workflows/{wid}")


def out(full, node, run=None):
    rd = full["data"]["resultData"]["runData"].get(node)
    if not rd:
        return []
    runs = rd if run is None else [rd[run]]
    items = []
    for r in runs:
        for branch in (r.get("data") or {}).get("main") or []:
            for it in branch or []:
                items.append(it["json"])
    return items


def runs(full, node):
    return len(full["data"]["resultData"]["runData"].get(node) or [])


# ---------------------------------------------------------------- сценарії

def answer(obj, finish="STOP", tokens=100):
    return {"candidates": [{"finishReason": finish, "content": {"parts": [{"text": json.dumps(obj)}]}}],
            "usageMetadata": {"totalTokenCount": tokens, "promptTokenCount": tokens, "candidatesTokenCount": 5},
            "modelVersion": "gemini-2.5-flash"}


def heard(text):
    return answer({"has_speech": bool(text), "transcription": text})


def judged(verdict, issues=None):
    return answer({"verdict": verdict, "issues": issues or []})


CSV_A = "\n".join([
    "file,language,speaker,start_s,end_s,duration_s,transcript,corrected_transcript",
    "audio/speaker_0/AAA___00h00m00.000---00h00m02.000.wav,ind,speaker_0,0,2,2,ini adalah teks,",
    "audio/speaker_0/AAA___00h00m02.000---00h00m04.000.wav,ind,speaker_0,2,4,2,satu dua tiga,",
    "audio/speaker_1/longid1234___00h00m00.000---00h00m02.000.wav,ind,speaker_1,0,2,2,\"halo, dunia\",",
    "audio/speaker_1/BBB___00h00m02.000---00h00m04.000.wav,ind,speaker_1,2,4,2,error case,",
    "audio/speaker_1/BBB___00h00m04.000---00h00m06.000.wav,ind,speaker_1,4,6,2,already done,",
    "audio/speaker_2/CCC___00h00m00.000---00h00m02.000.wav,ind,speaker_2,0,2,2,no audio,",
    "audio/speaker_2/DDD___00h00m09.000---00h00m10.000.wav,ind,speaker_2,9,10,1,hello there,",
]) + "\n"

# Що «чує» модель на кроці 1 (за id файлу на Drive) і що каже на кроці 2 (за еталоном)
HEARD_A = {"a1": heard("ini adalah teks"), "a2": heard("satu tiga"), "b1": heard("halo dunia"),
           "b2": {"error": {"message": "429 Resource exhausted"}}, "d1": heard("")}
JUDGE_A = {"satu dua tiga": judged("BRAK", ["'dua' missing"])}


def case_a(api, step=1):
    wf = build.build_sync()
    files = {
        "ROOT": [],
        "F0": [{"id": "a1", "name": "AAA___00h00m00.000---00h00m02.000.wav", "parents": ["F0"], "mimeType": "audio/wav"},
               {"id": "a2", "name": "AAA___00h00m02.000---00h00m04.000.wav", "parents": ["F0"], "mimeType": "audio/wav"},
               {"id": "x", "name": "desktop.ini", "parents": ["F0"], "mimeType": "text/plain"}],
        "F1": [{"id": "b1", "name": "5dd4fab2___00h00m00.000---00h00m02.000.wav", "mimeType": "audio/wav"},
               {"id": "b2", "name": "BBB___00h00m02.000---00h00m04.000.wav", "mimeType": "audio/wav"},
               {"id": "b3", "name": "BBB___00h00m04.000---00h00m06.000.wav", "mimeType": "audio/wav"}],
        "F2": [{"id": "d1", "name": "DDD___00h00m09.000---00h00m10.000.wav", "parents": ["F2"], "mimeType": "audio/wav"}],
    }
    payload = {
        "cfg": {"audio_folder_id": "ROOT", "batch_size": 2, "max_items": 0, "sample_step": step},
        "stubs": {
            "Drive: завантажити CSV": {"type": "binary", "text": CSV_A},
            "Drive: папка з аудіо": {"type": "items", "items": [
                {"id": "F0", "name": "speaker_0", "mimeType": "application/vnd.google-apps.folder"},
                {"id": "F1", "name": "speaker_1", "mimeType": "application/vnd.google-apps.folder"},
                {"id": "F2", "name": "speaker_2", "mimeType": "application/vnd.google-apps.folder"}]},
            "Drive: файли в папках": {"type": "perItem", "key": "id", "map": files},
            "Sheets: вже перевірені": {"type": "items", "items": [
                {"file": "audio/speaker_1/BBB___00h00m04.000---00h00m06.000.wav", "verdict": "OK", "match": "EXACT"},
                {"file": "audio/speaker_0/AAA___00h00m02.000---00h00m04.000.wav", "verdict": "ERROR", "match": "EXACT"}]},
            "Drive: завантажити аудіо": {"type": "audio"},
            "Gemini: транскрипція": {"type": "byField", "path": "row._file_id", "map": HEARD_A,
                                     "default": {"error": {"message": "stub: unknown file"}}},
            "Gemini: порівняння": {"type": "byField", "path": "row.transcript", "map": JUDGE_A,
                                   "default": judged("MINOR", ["stub default"])},
            "Sheets: записати результат": {"type": "echo"},
            "Sheets: рядки без аудіо": {"type": "echo"},
        },
    }
    full = run_case(api, wf, payload)
    checks = []
    status = full.get("status")
    key = lambda f: f.split("/")[-1].split("___")[0] + "@" + f.split("___")[-1][:12]
    if step > 1:  # рядки 0, 2, 4, 6: AAA@0 і longid перевіряються, BBB@4 вже готовий, DDD перевіряється
        rows = {key(r["file"]): r["verdict"] for r in out(full, "Sheets: записати результат")}
        checks.append(("sample: execution success", status == "success", status))
        checks.append(("sample: 3 rows checked", len(rows) == 3, rows))
        checks.append(("sample: no-audio row 5 not sampled", runs(full, "Sheets: рядки без аудіо") == 0, None))
        sm = out(full, "Підсумок")
        checks.append(("sample: plan step 2", sm and sm[0]["plan"].get("sample_step") == 2 and sm[0]["plan"].get("to_check") == 3, sm and sm[0]["plan"]))
        return checks
    checks.append(("execution success", status == "success", status))
    rows = {key(r["file"]): r for r in out(full, "Sheets: записати результат")}
    v = {k: (r["verdict"], r["match"], r["flag"]) for k, r in rows.items()}
    checks.append(("5 rows checked by AI", len(rows) == 5, sorted(v)))
    checks.append(("exact match -> OK by rule", v.get("AAA@00h00m00.000") == ("OK", "EXACT", ""), v.get("AAA@00h00m00.000")))
    checks.append(("ERROR row retried -> BRAK by judge", v.get("AAA@00h00m02.000") == ("BRAK", "EXACT", ""), v.get("AAA@00h00m02.000")))
    checks.append(("judge issues kept", "'dua' missing" in rows.get("AAA@00h00m02.000", {}).get("issues", ""), rows.get("AAA@00h00m02.000")))
    checks.append(("Bahasa-style id -> SPEAKER_TIMECODE, punctuation -> OK", v.get("longid1234@00h00m00.000") == ("OK", "SPEAKER_TIMECODE", ""), v.get("longid1234@00h00m00.000")))
    checks.append(("transcription HTTP error -> ERROR", (v.get("BBB@00h00m02.000") or ("",))[0] == "ERROR", rows.get("BBB@00h00m02.000")))
    checks.append(("silence -> BRAK 'no speech'", v.get("DDD@00h00m09.000", ("",))[0] == "BRAK" and "no speech" in rows.get("DDD@00h00m09.000", {}).get("issues", ""), rows.get("DDD@00h00m09.000")))
    checks.append(("done row skipped", "BBB@00h00m04.000" not in v, None))
    nm = out(full, "Sheets: рядки без аудіо")
    checks.append(("no-audio row -> SKIPPED/MISSING", [(r["verdict"], r["match"]) for r in nm] == [("SKIPPED", "MISSING")], nm))
    checks.append(("loop ran 3 batches", runs(full, "Gemini: транскрипція") == 3, runs(full, "Gemini: транскрипція")))
    br = [r for r in rows.values() if r["verdict"] == "BRAK" and r["wer"] != ""]
    checks.append(("WER from blind transcription", any(r["wer"] == 0.333 for r in br), [r["wer"] for r in br]))
    checks.append(("judge result ignored when rule decided", rows.get("AAA@00h00m00.000", {}).get("issues") == "", rows.get("AAA@00h00m00.000")))
    sm = out(full, "Підсумок")
    checks.append(("summary", sm and sm[0]["checked"].get("total") == 5 and sm[0]["plan"].get("already_done") == 1, sm))
    return checks


def stage1_lines(meta, heard_by_ref):
    lines = []
    for m in meta:
        if m["uri"] and m["transcript"] in heard_by_ref:
            a = heard_by_ref[m["transcript"]]
            line = {"request": {"contents": [{"role": "user", "parts": [{"fileData": {"mimeType": "audio/wav", "fileUri": m["uri"]}}, {"text": "t"}]}]}}
            if "status" in a:
                line["status"] = a["status"]
            else:
                line["response"] = a
            lines.append(json.dumps(line))
    return lines


def case_b(api, scenario):
    wf = build.build_batch()
    bucket = "test-bucket"
    names = ["Bahasa/audio/speaker_0/AAA___00h00m00.000---00h00m02.000.wav",
             "Bahasa/audio/speaker_0/AAA___00h00m02.000---00h00m04.000.wav",
             "Bahasa/audio/speaker_1/5dd4fab2___00h00m00.000---00h00m02.000.wav",
             "Bahasa/audio/speaker_1/BBB___00h00m02.000---00h00m04.000.wav",
             "Bahasa/audio/speaker_1/BBB___00h00m04.000---00h00m06.000.wav",
             "Bahasa/audio/speaker_2/DDD___00h00m09.000---00h00m10.000.wav",
             "Bahasa/audio/speaker_9/desktop.ini"]
    import csv as _csv
    import io
    rows = list(_csv.DictReader(io.StringIO(CSV_A)))
    files = [{"id": f"gs://{bucket}/{n}", "path": n[len("Bahasa/audio/"):]} for n in names if n.endswith(".wav")]
    res, _ = q.match_rows(rows, files)
    meta = [{"file": rows[r["row"]]["file"], "speaker": rows[r["row"]]["speaker"], "transcript": rows[r["row"]]["transcript"],
             "match": r["status"], "uri": r["file_id"] or ""} for r in res]
    # крок 1: що «почула» модель (за еталоном рядка, щоб зручно задавати)
    heard_by_ref = {"ini adalah teks": heard("ini adalah teks"),            # точний збіг -> OK правилом
                    "satu dua tiga": heard("satu tiga"),                    # -> крок 2: BRAK
                    "halo, dunia": heard("halo dunya"),                     # -> крок 2: MINOR
                    "error case": {"status": "RESOURCE_EXHAUSTED"},         # -> ERROR
                    "already done": {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}},  # -> ERROR
                    "hello there": heard("")}                               # тиша -> BRAK правилом
    lines1 = stage1_lines(meta, heard_by_ref)
    interim = []
    for m in meta:
        if not m["uri"]:
            interim.append(dict(m, tr=None, decided=None))
            continue
        a = heard_by_ref.get(m["transcript"])
        tr = q.parse_transcription(a.get("response", a) if a and "status" not in a else None, a.get("status") if a else None) if a \
            else {"ok": False, "has_speech": False, "transcription": "", "error": "no result in batch output"}
        interim.append(dict(m, tr=tr, decided=q.decide(m["transcript"], tr)))
    judge_ans = {"satu dua tiga": judged("BRAK", ["'dua' missing"]), "halo, dunia": judged("MINOR", ["dunya vs dunia"])}
    lines2 = []
    for i, m in enumerate(interim):
        if m["uri"] and not m["decided"]:
            req = q.build_judge_line(i, m["transcript"], m["tr"]["transcription"], "Indonesian")["request"]
            lines2.append(json.dumps({"request": req, "response": judge_ans[m["transcript"]]}))
    step1 = [{"ok": True, "stage": "1", "data": "\n".join(lines1 + [lines1[0], "{broken"]) + "\n"}]
    step2 = [{"ok": True, "stage": "2", "data": "\n".join(lines2) + "\n"}]
    if scenario == "failed":
        step2 = [{"ok": False, "stage": "2", "phase": "failed", "state": "JOB_STATE_FAILED", "error": "quota", "job_name": "jobs/2"}]
    elif scenario == "timeout":
        step1 = [{"ok": False, "stage": "1", "phase": "timeout", "state": "JOB_STATE_RUNNING", "error": "", "job_name": "jobs/1"}]
    payload = {
        "cfg": {"bucket": bucket},
        "stubs": {
            "GCS: завантажити CSV": {"type": "binary", "text": CSV_A},
            "GCS: список аудіо": {"type": "items", "items": [{"items": [{"name": n} for n in names[:4]], "nextPageToken": "x"},
                                                              {"items": [{"name": n} for n in names[4:]]}]},
            "Крок 1: задача Vertex": {"type": "items", "items": step1},
            "Крок 2: задача Vertex": {"type": "items", "items": step2},
            "GCS: завантажити rows.json": {"type": "text", "text": json.dumps(meta)},
            "GCS: завантажити interim.json": {"type": "text", "text": json.dumps(interim)},
        },
    }
    full = run_case(api, wf, payload)
    checks = []
    st = full.get("status")
    if scenario == "ok":
        checks.append(("execution success", st == "success", st))
        mt = out(full, "Зіставлення і пакет")
        checks.append(("6 transcription requests, 1 missing", mt and mt[0]["requests"] == 6 and mt[0]["match"].get("MISSING") == 1, mt and mt[0]))
        s2 = out(full, "Транскрипції і пакет 2")
        checks.append(("2 judge requests, 4 decided by rules", s2 and s2[0]["judge_requests"] == 2 and s2[0]["decided_by_rules"] == 4, s2 and s2[0]))
        rp = out(full, "Звіт")
        s = rp[0]["summary"] if rp else {}
        exp = {"total": 7, "verdict:OK": 1, "verdict:BRAK": 2, "verdict:MINOR": 1, "verdict:ERROR": 2, "verdict:SKIPPED": 1,
               "match:SPEAKER_TIMECODE": 1, "duplicate_results": 1, "broken_lines": 1, "judge_requests": 2}
        bad = {k: (s.get(k), v) for k, v in exp.items() if s.get(k) != v}
        checks.append(("report summary", not bad, bad or s))
        checks.append(("mail sent", runs(full, "Gmail: звіт готовий") == 1, None))
        checks.append(("both batch inputs deleted", len(out(full, "GCS: видалити пакет")) == 2, len(out(full, "GCS: видалити пакет"))))
        checks.append(("no SSH / shell nodes", not any(n["type"] == "n8n-nodes-base.ssh" for n in wf["nodes"]), None))
    else:
        checks.append(("execution error", st == "error", st))
        checks.append(("error mail", runs(full, "Gmail: помилка") == 1, None))
        msg = out(full, "Текст помилки")
        want = "quota" if scenario == "failed" else "скасовано"
        checks.append(("error text", msg and want in msg[0]["message"], msg))
        checks.append(("no report", runs(full, "Звіт") == 0, None))
        checks.append(("failed at the right step", msg and ("крок 2" if scenario == "failed" else "крок 1") in msg[0]["message"], msg))
    return checks


def case_job(api, scenario):
    """Підфлоу «Задача Vertex»: успіх (з одним очікуванням), FAILED, таймаут."""
    wf = build.build_vertex_job()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    name = "projects/p/locations/us-central1/batchPredictionJobs/7"
    out_dir = "gs://b/run/results1/prediction-model-7"
    if scenario == "ok":
        seq = [[{"state": "JOB_STATE_RUNNING", "name": name, "createTime": now}],
               [{"state": "JOB_STATE_SUCCEEDED", "name": name, "createTime": now, "outputInfo": {"gcsOutputDirectory": out_dir},
                 "completionStats": {"successfulCount": "2"}}]]
    elif scenario == "failed":
        seq = [[{"state": "JOB_STATE_FAILED", "name": name, "createTime": now, "error": {"message": "quota"}}]]
    else:
        seq = [[{"state": "JOB_STATE_RUNNING", "name": name, "createTime": "2020-01-01T00:00:00Z"}]]
    payload = {
        "input": {"stage": "1", "gcp_project": "p", "gcp_location": "us-central1", "bucket": "b", "model": "gemini-2.5-flash",
                  "input_object": "run/batch1.jsonl", "output_prefix": "run/results1/", "display_name": "t",
                  "poll_minutes": 1, "max_wait_hours": 24},
        "stubs": {
            "Vertex: створити задачу": {"type": "items", "items": [{"name": name, "createTime": now}]},
            "Vertex: стан задачі": {"type": "sequence", "seq": seq},
            "GCS: список результатів": {"type": "items", "items": [{"items": [
                {"name": "run/results1/prediction-model-7/predictions_000.jsonl"},
                {"name": "run/results1/prediction-model-7/predictions_001.jsonl"},
                {"name": "run/results1/prediction-model-7/README.txt"}]}]},
            "GCS: завантажити результат": {"type": "byName", "files": {
                "run/results1/prediction-model-7/predictions_000.jsonl": "line-a\n",
                "run/results1/prediction-model-7/predictions_001.jsonl": "line-b\n"}},
        },
    }
    full = run_case(api, wf, payload)
    checks = []
    res = out(full, "Результат: успіх") if scenario == "ok" else out(full, "Результат: помилка")
    checks.append(("execution success", full.get("status") == "success", full.get("status")))
    if scenario == "ok":
        checks.append(("polled twice", runs(full, "Vertex: стан задачі") == 2, runs(full, "Vertex: стан задачі")))
        checks.append(("2 result files, ok=true", [r.get("ok") for r in res] == [True, True] and [r["data"] for r in res] == ["line-a\n", "line-b\n"], res))
        checks.append(("results listed under job output", out(full, "Стан задачі")[-1]["out_prefix"] == "run/results1/prediction-model-7/", out(full, "Стан задачі")))
        checks.append(("not cancelled", runs(full, "Vertex: скасувати задачу") == 0, None))
    else:
        checks.append(("ok=false with phase", res and res[0]["ok"] is False and res[0]["phase"] == scenario, res))
        checks.append(("cancel called", runs(full, "Vertex: скасувати задачу") == 1, None))
        checks.append(("no results read", runs(full, "GCS: список результатів") == 0, None))
    return checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--key-file", required=True)
    a = ap.parse_args()
    api = Api(a.url, Path(a.key_file).read_text(encoding="utf-8").strip())
    total = fails = 0
    for title, fn in (("A sync", lambda: case_a(api)), ("A sample_step=2", lambda: case_a(api, 2)), ("B ok", lambda: case_b(api, "ok")),
                      ("B failed", lambda: case_b(api, "failed")), ("B timeout", lambda: case_b(api, "timeout")),
                      ("Vertex job ok", lambda: case_job(api, "ok")), ("Vertex job failed", lambda: case_job(api, "failed")),
                      ("Vertex job timeout", lambda: case_job(api, "timeout"))):
        print("==", title)
        for name, ok, info in fn():
            total += 1
            fails += 0 if ok else 1
            print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else "  -> " + json.dumps(info, ensure_ascii=False, default=str)[:400]))
    print(f"RESULT: {total - fails}/{total}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
